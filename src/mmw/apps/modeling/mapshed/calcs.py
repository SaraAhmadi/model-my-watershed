# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import

import math

from collections import namedtuple

from gwlfe.enums import GrowFlag

from django.conf import settings
from django.db import connection

NRur = settings.GWLFE_DEFAULTS['NRur']
NUM_WEATHER_STATIONS = settings.GWLFE_CONFIG['NumWeatherStations']
KV_FACTOR = settings.GWLFE_CONFIG['KvFactor']
MONTHS = settings.GWLFE_DEFAULTS['Month']
MONTHDAYS = settings.GWLFE_CONFIG['MonthDays']
WEIGHTOF = settings.GWLFE_CONFIG['AvgAnimalWt']
LIVESTOCK = settings.GWLFE_CONFIG['Livestock']
POULTRY = settings.GWLFE_CONFIG['Poultry']
M3_PER_MGAL = 3785.41178
AG_NLCD_CODES = settings.GWLFE_CONFIG['AgriculturalNLCDCodes']
KM_PER_M = 0.001
CM_PER_INCH = 2.54
CM_PER_M = 100.0


def day_lengths(geom):
    """
    Given a geometry in EPSG:4326, returns an array of 12 floats, each
    representing the average number of daylight hours at that geometry's
    centroid for each month.

    Original at Class1.vb@1.3.0:8878-8889
    """
    latitude = geom.centroid[1]
    lengths = [0.0] * 12

    for m in range(12):
        # Magic formula taken from original MapShed source
        lengths[m] = 7.63942 * math.acos(0.43481 *
                                         math.tan(0.017453 * latitude) *
                                         math.cos(0.0172 *
                                                  ((m + 1) * 30.4375 - 5)))

    return [round(l, 1) for l in lengths]


def nearest_weather_stations(geom, n=NUM_WEATHER_STATIONS):
    """
    Given a geometry, returns a list of the n closest weather stations to it
    """
    sql = '''
          SELECT station, location, meanrh, meanwind, meanprecip,
                 begyear, endyear, eroscoeff, rain_cool, rain_warm,
                 etadj, grw_start, grw_end
          FROM ms_weather_station
          ORDER BY geom <-> ST_SetSRID(ST_GeomFromText(%s), 4326)
          LIMIT %s;
          '''

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt, n])

        if cursor.rowcount == 0:
            raise Exception("No weather stations found.")

        # Return all rows from cursor as namedtuple
        weather_station = namedtuple('WeatherStation',
                                     [col[0] for col in cursor.description])
        return [weather_station(*row) for row in cursor.fetchall()]


def growing_season(ws):
    """
    Given an array of weather stations, returns an array of 12 integers, each 1
    or 0, indicating whether it is a growing season or not respectively.
    We adopt a liberal approach, unioning the ranges to get a superset which is
    a growing season for any weather station.
    """

    start = min([MONTHS.index(w.grw_start) for w in ws])
    end = max([MONTHS.index(w.grw_end) for w in ws])

    season = [GrowFlag.NON_GROWING_SEASON] * 12
    season[start:end] = [GrowFlag.GROWING_SEASON] * (end - start)

    return season


def erosion_coeff(ws, season):
    """
    Given an array of weather stations and a growing season array, returns an
    array of 12 decimals, one for the erosion coefficient of each month. For
    months that are in the growing season, we average the `rain_warm` of both
    the weather stations, and for months outside the growing season, we average
    `rain_cool` instead.
    """

    avg_warm = float(sum([w.rain_warm for w in ws])) / len(ws)
    avg_cool = float(sum([w.rain_cool for w in ws])) / len(ws)

    return [avg_warm if month == GrowFlag.GROWING_SEASON else avg_cool
            for month in season]


def et_adjustment(ws):
    """
    Given an array of weather stations, returns an array of 12 decimals, one
    for the ET Adjustment of each month. We average the `etadj` of all weather
    stations, and use that value for all months.
    """

    avg_etadj = float(sum([w.etadj for w in ws])) / len(ws)

    return [avg_etadj] * 12


def kv_coefficient(area_pcts, season):
    """
    Given arrays of land use area percentages and growing season, returns an
    array of 12 floats, one for the KV coefficient of each month. The KV of a
    month is initialized to the sum of ETs of each land use type weighted by
    its area percent, using growth or dormant coefficients depending on whether
    or not the month is in the growing season. The value is then averaged over
    the preceeding month and multiplied by the KV Factor. January being the
    first month is not averaged.

    Original at Class1.vb@1.3.0:4989-4995
    """

    et_grow = sum([et * area_pct for et, area_pct in
                  zip(settings.GWLFE_CONFIG['ETGrowCoeff'], area_pcts)])
    et_dorm = sum([et * area_pct for et, area_pct in
                  zip(settings.GWLFE_CONFIG['ETDormCoeff'], area_pcts)])

    kv = [et_grow if m == GrowFlag.GROWING_SEASON else et_dorm for m in season]
    kv[0] *= KV_FACTOR
    for m in range(1, 12):
        kv[m] = KV_FACTOR * (kv[m] + kv[m-1]) / 2

    return kv


def animal_energy_units(geom):
    """
    Given a geometry, returns the total livestock and poultry AEUs within it

    Original at Class1.vb@1.3.0:9230-9247
    """
    sql = '''
          WITH clipped_counties AS (
              SELECT ST_Intersection(geom,
                                     ST_SetSRID(ST_GeomFromText(%s),
                                                4326)) AS geom_clipped,
                     ms_county_animals.*
              FROM ms_county_animals
              WHERE ST_Intersects(geom,
                                  ST_SetSRID(ST_GeomFromText(%s),
                                             4326))
          ), clipped_counties_with_area AS (
              SELECT ST_Area(geom_clipped) / ST_Area(geom) AS clip_percent,
                     clipped_counties.*
              FROM clipped_counties
          )
          SELECT SUM(beef_ha * totalha * clip_percent) AS beef_cows,
                 SUM(broiler_ha * totalha * clip_percent) AS broilers,
                 SUM(dairy_ha * totalha * clip_percent) AS dairy_cows,
                 SUM(goat_ha * totalha * clip_percent) +
                 SUM(sheep_ha * totalha * clip_percent) AS sheep,
                 SUM(hog_ha * totalha * clip_percent) AS hogs,
                 SUM(horse_ha * totalha * clip_percent) AS horses,
                 SUM(layer_ha * totalha * clip_percent) AS layers,
                 SUM(turkey_ha * totalha * clip_percent) AS turkeys
          FROM clipped_counties_with_area;
          '''

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt, geom.wkt])

        # Convert result to dictionary
        columns = [col[0] for col in cursor.description]
        values = cursor.fetchone()  # Only one row since aggregate query
        population = dict(zip(columns, values))

        livestock_aeu = round(sum(population.get(animal, 0) *
                                  WEIGHTOF[animal] / 1000
                                  for animal in LIVESTOCK))
        poultry_aeu = round(sum(population.get(animal, 0) *
                                WEIGHTOF[animal] / 1000
                                for animal in POULTRY))

        return livestock_aeu, poultry_aeu, population


def manure_spread(aeu):
    """
    Given Animal Energy Units, returns two lists, containing nitrogen and
    phosphorus manure spreading values for each of the manure spreading land
    use types.
    """
    num_land_uses = len(settings.GWLFE_CONFIG['ManureSpreadingLandUseIndices'])

    if 1.0 <= aeu:
        n_spread, p_spread = 4.88, 0.86
    elif 0.5 < aeu < 1.0:
        n_spread, p_spread = 3.66, 0.57
    else:
        n_spread, p_spread = 2.44, 0.38

    return [n_spread] * num_land_uses, [p_spread] * num_land_uses


def ag_ls_c_p(geom):
    """
    Given a geometry, calculates the area-weighted average value of LS, C, and
    P factors for agriculatural land use tyeps within the geometry, namely
    Hay/Pasture and Cropland.
    """
    sql = '''
          WITH clipped_counties AS (
              SELECT ST_Intersection(geom,
                                     ST_SetSRID(ST_GeomFromText(%s),
                                                4326)) AS geom_clipped,
                     ms_county_animals.*
              FROM ms_county_animals
              WHERE ST_Intersects(geom,
                                  ST_SetSRID(ST_GeomFromText(%s),
                                             4326))
          ), clipped_counties_with_area AS (
              SELECT ST_Area(geom_clipped) /
                     ST_Area(ST_SetSRID(ST_GeomFromText(%s),
                                        4326)) AS clip_percent,
                     clipped_counties.*
              FROM clipped_counties
          )
          SELECT SUM(hp_ls * clip_percent) AS hp_ls,
                 SUM(hp_c * clip_percent) AS hp_c,
                 SUM(hp_p * clip_percent) AS hp_p,
                 SUM(crop_ls * clip_percent) AS crop_ls,
                 SUM(crop_c * clip_percent) AS crop_c,
                 SUM(crop_p * clip_percent) AS crop_p
          FROM clipped_counties_with_area;
          '''

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt, geom.wkt, geom.wkt])

        ag_lscp = namedtuple('Ag_LS_C_P',
                             [col[0] for col in cursor.description])

        return ag_lscp(*(cursor.fetchone()))


def ls_factors(lu_strms, total_strm_len, areas, avg_slope, ag_lscp):
    results = [0.0] * len(lu_strms)
    if 0 <= avg_slope <= 1.0:
        m = 0.2
    elif 1.0 < avg_slope <= 3.5:
        m = 0.3
    elif 3.5 < avg_slope <= 4.5:
        m = 0.4
    else:
        m = 0.5

    results[0] = ag_lscp.hp_ls
    results[1] = ag_lscp.crop_ls

    for i in xrange(2, 16):
        results[i] = (ls_factor(lu_strms[i] * total_strm_len * KM_PER_M,
                      areas[i], avg_slope, m))

    return results


def ls_factor(stream_length, area, avg_slope, m):
    #  units --> stream_length:km, area:hectare, avg_slope:%, m:(n/a)
    if area == 0.0:
        return 0.0

    slope_cell_size = 30

    #  ensure a floor on the stream length of 250m
    if stream_length < (250.0 * KM_PER_M):
        slope_length = (0.5 * area * 10000) / 250.0
    else:
        slope_length = (0.5 * area) / stream_length

    ls = (((slope_length / 22.13) ** m) *
          (0.065 + (0.043 * avg_slope) + (0.0065 * (avg_slope ** 2))))

    #  keep this for posterity
    if slope_cell_size < 50:
        return (0.0793 * ls) + 0.1913
    else:
        return ls


def stream_length(geom, drb=False):
    """
    Given a geometry, finds the total length of streams in meters within it.
    If the drb flag is set, we use the Delaware River Basin dataset instead
    of NHD Flowline.
    """
    sql = '''
          SELECT ROUND(SUM(ST_Length(
              ST_Transform(
                  ST_Intersection(geom,
                                  ST_SetSRID(ST_GeomFromText(%s), 4326)),
                  5070))))
          FROM {datasource}
          WHERE ST_Intersects(geom,
                              ST_SetSRID(ST_GeomFromText(%s), 4326));
          '''.format(datasource='drb_streams_50' if drb else 'nhdflowline')

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt, geom.wkt])

        return cursor.fetchone()[0] or 0  # Aggregate query returns singleton


def streams(geom, drb=False):
    """
    Given a geometry, returns a list of GeoJSON objects, either LineStrings or
    MultiLineStrings, representing the set of streams contained inside the
    geometry, in LatLng. If the drb flag is set, we use the Delaware River
    Basin dataset instead of NHD Flowline.
    """
    sql = '''
          WITH clipped_streams AS (
              SELECT ST_Intersection(geom,
                                     ST_SetSRID(ST_GeomFromText(%s), 4326))
                     AS stream
              FROM {datasource}
              WHERE ST_Intersects(geom,
                                  ST_SetSRID(ST_GeomFromText(%s), 4326))
          )
          SELECT ST_AsGeoJSON(ST_Force2D(stream))
          FROM clipped_streams
          WHERE NOT ST_IsEmpty(stream)
          '''.format(datasource='drb_streams_50' if drb else 'nhdflowline')

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt, geom.wkt])

        return [row[0] for row in cursor.fetchall()]  # List of GeoJSON strings


def point_source_discharge(geom, area):
    """
    Given a geometry and its area in square meters, returns three lists,
    each with 12 values, one for each month, containing the Nitrogen Load (in
    kg), Phosphorus Load (in kg), and Discharge (in centimeters per month).
    """
    sql = '''
          SELECT SUM(mgd) AS mg_d,
                 SUM(kgn_yr) / 12 AS kgn_month,
                 SUM(kgp_yr) / 12 AS kgp_month
          FROM ms_pointsource
          WHERE ST_Intersects(geom,
                              ST_SetSRID(ST_GeomFromText(%s), 4326));
          '''

    with connection.cursor() as cursor:
        cursor.execute(sql, [geom.wkt])
        mg_d, kgn_month, kgp_month = cursor.fetchone()

        n_load = [float(kgn_month)] * 12 if kgn_month else [0.0] * 12
        p_load = [float(kgp_month)] * 12 if kgp_month else [0.0] * 12
        discharge = [float(mg_d) * days * M3_PER_MGAL * CM_PER_M / area
                     for days in MONTHDAYS] if mg_d else [0.0] * 12

        return n_load, p_load, discharge


def weather_data(ws, begyear, endyear):
    """
    Given a list of Weather Stations and beginning and end years, returns two
    3D arrays, one for average temperature and the other for precipitation,
    for each day in each month in each year in the range, averaged over all
    stations in the list, in the format:
        array[year][month][day] = value
    where `year` 0 corresponds to the first year in the range, 1 to the second,
    and so on; `month` 0 corresponds to January, 1 to February, and so on;
    `day` 0 corresponds to the 1st of the month, 1 to the 2nd, and so on.
    """
    # Utility function to convert Fahrenheit to Celsius
    def f_to_c(f):
        return (f - 32) * 5.0 / 9.0

    temp_sql = '''
               SELECT year, EXTRACT(MONTH FROM TO_DATE(month, 'MON')) AS month,
                      AVG("1") AS "1", AVG("2") AS "2", AVG("3") AS "3",
                      AVG("4") AS "4", AVG("5") AS "5", AVG("6") AS "6",
                      AVG("7") AS "7", AVG("8") AS "8", AVG("9") AS "9",
                      AVG("10") AS "10", AVG("11") AS "11", AVG("12") AS "12",
                      AVG("13") AS "13", AVG("14") AS "14", AVG("15") AS "15",
                      AVG("16") AS "16", AVG("17") AS "17", AVG("18") AS "18",
                      AVG("19") AS "19", AVG("20") AS "20", AVG("21") AS "21",
                      AVG("22") AS "22", AVG("23") AS "23", AVG("24") AS "24",
                      AVG("25") AS "25", AVG("26") AS "26", AVG("27") AS "27",
                      AVG("28") AS "28", AVG("29") AS "29", AVG("30") AS "30",
                      AVG("31") AS "31"
               FROM ms_weather
               WHERE station IN %s
                 AND measure IN ('TMax', 'TMin')
                 AND year BETWEEN %s AND %s
               GROUP BY year, month
               ORDER BY year, month;
               '''
    prcp_sql = '''
               SELECT year, EXTRACT(MONTH FROM TO_DATE(month, 'MON')) AS month,
                     "1",  "2",  "3",  "4",  "5",  "6",  "7",  "8",  "9", "10",
                    "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
                    "21", "22", "23", "24", "25", "26", "27", "28", "29", "30",
                    "31"
               FROM ms_weather
               WHERE station IN %s
                 AND measure = 'Prcp'
                 AND year BETWEEN %s AND %s
               ORDER BY year, month;
               '''

    year_range = endyear - begyear + 1
    stations = tuple([w.station for w in ws])
    temps = [[[0] * 31 for m in range(12)] for y in range(year_range)]
    prcps = [[[0] * 31 for m in range(12)] for y in range(year_range)]

    with connection.cursor() as cursor:
        cursor.execute(temp_sql, [stations, begyear, endyear])
        for row in cursor.fetchall():
            year = int(row[0]) - begyear
            month = int(row[1]) - 1
            for day in range(31):
                temps[year][month][day] = f_to_c(float(row[day + 2]))

    with connection.cursor() as cursor:
        cursor.execute(prcp_sql, [stations, begyear, endyear])
        for row in cursor.fetchall():
            year = int(row[0]) - begyear
            month = int(row[1]) - 1
            for day in range(31):
                prcps[year][month][day] = float(row[day + 2]) * CM_PER_INCH

    return temps, prcps


def curve_number(n_count, ng_count):
    """
    Given a dictionary mapping NLCD codes to counts of cells, and another
    mapping pairs of NLCD codes and Hydrological Soil Groups, returns the
    curve number for each non-urban MapShed land use type by calculating the
    average hydrological soil group for each land use type, rounded to the
    nearest integer, and looking up the value in the CURVE_NUMBER table.

    Original at Class1.vb@1.3.0:7257-7267
    """

    # Calculate average hydrological soil group for each NLCD type by
    # reducing [(n, g): c] to [n: avg(g * c)]
    n_gavg = {}
    for (n, g), count in ng_count.iteritems():
        n_gavg[n] = float(g) * count / n_count[n] + n_gavg.get(n, 0)

    def cni(nlcd):
        # Helper method to lookup values from CURVE_NUMBER table
        return settings.CURVE_NUMBER[nlcd][int(round(n_gavg.get(nlcd, 0)))]

    def cni_avg(nlcds):
        # Helper method to average non-zero values only
        vals = [cni(nlcd) for nlcd in nlcds]
        sum_vals = sum(vals)
        nonzero_vals = len([v for v in vals if v > 0])

        return float(sum_vals) / nonzero_vals if nonzero_vals > 0 else 0

    return [
        cni(81),  # Hay/Pasture
        cni(82),  # Cropland
        cni_avg([41, 42, 43, 52]),  # Forest
        cni_avg([90, 95]),  # Wetland
        0,  # Disturbed
        0,  # Turf Grass
        cni(71),  # Open Land
        cni_avg([12, 31]),  # Bare Rock
        0,  # Sandy Areas
        0,  # Unpaved Road
        0, 0, 0, 0, 0, 0  # Urban Land Use Types
    ]


def sediment_phosphorus(nt_count):
    """
    Given a dictionary mapping pairs of NLCD Codes and Soil Textures to counts
    of cells, returns the average concentration of phosphorus in the soil by
    looking up the value in the SOILP table. NLCD Codes 81 and 82 correspond to
    agricultural values, and the rest to non-agricultural ones.

    Original at Class1.vb@1.3.0:8975-8988
    """

    ag_textures = {}
    nag_textures = {}
    total = sum(nt_count.values())

    for (n, t), count in nt_count.iteritems():
        if n in AG_NLCD_CODES:
            ag_textures[t] = count + ag_textures.get(t, 0)
        else:
            nag_textures[t] = count + nag_textures.get(t, 0)

    ag_sedp = sum(count * settings.SOILP[t][0]
                  for t, count in ag_textures.iteritems())
    nag_sedp = sum(count * settings.SOILP[t][1]
                   for t, count in nag_textures.iteritems())

    sedp = float(ag_sedp + nag_sedp) / total

    return sedp * 1.6


def groundwater_nitrogen_conc(gwn_dict):
    """
    Calculate GwN and GwP from a dictionary of NLCD land use keys
    paired with the number of cells as values

    Original at Class1.vb@1.3.0:9007-9022
    """

    # Discard any key-value pairs for keys < 1
    valid_res = {k: gwn_dict[k] for k in gwn_dict.keys() if k > 0}
    # Combine values for all keys >= 20 onto one key
    valid_res[20] = sum([gwn_dict[k] for k in valid_res.keys() if k >= 20])
    valid_total_cells = sum([v for v in valid_res.values()])

    weighted_conc = 0
    if valid_total_cells > 0:
        weighted_conc = sum([float(gwn * count)/valid_total_cells
                             for gwn, count in valid_res.iteritems()])

    groundwater_nitrogen_conc = (0.7973 * weighted_conc) - 0.692
    groundwater_phosphorus_conc = (0.0049 * weighted_conc) + 0.0089

    if groundwater_nitrogen_conc < 0.34:
        groundwater_nitrogen_conc = 0.34

    return groundwater_nitrogen_conc, groundwater_phosphorus_conc


def sediment_delivery_ratio(area_sq_km):
    """
    Calculate Sediment Delivery Ratio from the basin area in square km

    Original at Class1.vb@1.3.0:9334-9340
    """

    if area_sq_km < 50:
        return (0.000005 * (area_sq_km ** 2) -
                (0.0014 * area_sq_km) + 0.198)
    else:
        return 0.451 * (area_sq_km ** (0 - 0.298))


def landuse_pcts(n_count):
    """
    Given a dictionary mapping NLCD Codes to counts of cells, returns an
    array mapping MapShed Land Use Types (as the index) to percent
    area covered.
    """

    total = sum(n_count.values())
    if total > 0:
        n_pct = {nlcd: float(count) / total
                 for nlcd, count in n_count.iteritems()}
    else:
        n_pct = {nlcd: 0 for nlcd in n_count.keys()}

    return [
        n_pct.get(81, 0),  # Hay/Pasture
        n_pct.get(82, 0),  # Cropland
        n_pct.get(41, 0) + n_pct.get(42, 0) +
        n_pct.get(43, 0) + n_pct.get(52, 0),  # Forest
        n_pct.get(90, 0) + n_pct.get(95, 0),  # Wetland
        0,  # Disturbed
        0,  # Turf Grass
        n_pct.get(71, 0),  # Open Land
        n_pct.get(12, 0) + n_pct.get(31, 0),  # Bare Rock
        0,  # Sandy Areas
        0,  # Unpaved Road
        n_pct.get(21, 0) + n_pct.get(22, 0),  # Low Density Mixed
        n_pct.get(23, 0),  # Medium Density Mixed
        n_pct.get(24, 0),  # High Density Mixed
        0,  # Low Density Residential
        0,  # Medium Density Residential
        0,  # High Density Residential
    ]


def num_normal_sys(lu_area):
    """
    Given the land use area in hectares, estimates the number of normal septic
    systems based on the constants SSLDR and SSLDM for Residential and Mixed
    land uses respectively.
    Since in this version Residential land uses are always 0, the value is
    effectively dependent only on the area of medium density mixed land use.
    However, we replicate the original formula for consistency.

    Returns an array with an integer value for each month of the
    year as input for GWLF-E.

    Original at Class1.vb@1.3.0:9577-9579
    """

    SSLDR = settings.GWLFE_CONFIG['SSLDR']
    SSLDM = settings.GWLFE_CONFIG['SSLDM']

    normal_sys_estimate = SSLDR * lu_area[14] + SSLDM * lu_area[11]
    normal_sys_int = int(round(normal_sys_estimate))
    return [normal_sys_int for n in xrange(12)]


def sed_a_factor(landuse_pct_vals, cn, AEU, AvKF, AvSlope):
    # see Class1.vb#10518
    urban_pct = sum(landuse_pct_vals[NRur:])

    # see Class1.vb#10512
    avg_cn = 0
    nonzero_pct_sum = 0
    for (cn_val, pct) in zip(cn, landuse_pct_vals):
        avg_cn += cn_val * pct
        if cn_val != 0:
            nonzero_pct_sum += pct
    # We need to normalize since the land uses with cn_val ==0
    # shouldn't be counted as part of the average.
    avg_cn = avg_cn / nonzero_pct_sum if nonzero_pct_sum > 0 else 0.0

    # see Class1.vb#10521
    return ((0.00467 * urban_pct) + (0.000863 * AEU) +
            (0.000001 * avg_cn) + (0.000425 * AvKF) +
            (0.000001 * AvSlope) - 0.000036)


def p_factors(avg_slope, ag_lscp):
    """
    Given the average slope, calculates the P Factor for rural land use types.

    Original at Class1.vb@1.3.0:4393-4470
    """
    if 0 <= avg_slope < 2.1:
        ag_p = 0.52
    elif 2.1 <= avg_slope < 7.1:
        ag_p = 0.45
    elif 7.1 <= avg_slope < 12.1:
        ag_p = 0.52
    elif 12.1 <= avg_slope < 18.1:
        ag_p = 0.66
    else:
        ag_p = 0.74

    return [
        ag_lscp.hp_p,    # Hay/Pasture
        ag_lscp.crop_p,  # Cropland
        ag_p,  # Forest
        0.1,   # Wetland
        0.1,   # Disturbed
        0.2,   # Turf Grass
        ag_p,  # Open Land
        ag_p,  # Bare Rock
        ag_p,  # Sandy Areas
        1.0,   # Unpaved
        0, 0, 0, 0, 0, 0  # Urban Land Use Types
    ]


def phosphorus_conc(sed_phos):
    """
    Given the average concentration of phosphorus dissolved in sediments for
    the entire polygon, returns average concentration per land use type based
    on pre-baked estimates.

    Original at Class1.vb@1.3.0:8975-9001,9350-9359
    """
    psed = sed_phos / 1.6
    stp = 190 * psed / 836
    prunoff = (1.98 * stp + 79) / 1000
    prunoff_turf = 2.9 * psed / 836

    return [
        prunoff,       # Hay/Pasture
        prunoff,       # Cropland
        0.01,          # Forest
        0.01,          # Wetland
        0.01,          # Disturbed
        prunoff_turf,  # Turf Grass
        0.01,          # Open Land
        0.01,          # Bare Rock
        0.01,          # Sandy Areas
        0.01,          # Unpaved
        0, 0, 0, 0, 0, 0  # Urban Land Use Types
    ]
