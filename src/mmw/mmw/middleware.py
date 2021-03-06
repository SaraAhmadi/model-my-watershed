from django.conf import settings
from django.core.handlers.base import BaseHandler
from django.core import urlresolvers


def bypass_middleware(view):
    view.bypass_middleware = True

    return view


class BypassMiddleware(object):
    """
    Customized version of a gist detailing this technique by @bryanhelmig

    See also: https://gist.github.com/bryanhelmig/9d09d1bd9a63504371d2
    """
    def process_request(self, request):
        """Replicates a lot of code from BaseHandler#get_response."""
        # Setup URL resolver
        urlconf = settings.ROOT_URLCONF
        urlresolvers.set_urlconf(urlconf)
        resolver = urlresolvers.RegexURLResolver(r'^/', urlconf)
        callback, callback_args, \
            callback_kwargs = resolver.resolve(request.path_info)

        if getattr(callback, 'bypass_middleware', False):
            # bypass_middleware decorator was used; zero out all
            # middleware and return the response.
            handler = BaseHandler()

            handler._request_middleware = []
            handler._view_middleware = []
            handler._template_response_middleware = []
            handler._response_middleware = []
            handler._exception_middleware = []

            response = handler.get_response(request)

            return response
