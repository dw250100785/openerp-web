# -*- coding: utf-8 -*-
#----------------------------------------------------------
# OpenERP Web HTTP layer
#----------------------------------------------------------
import ast
import cgi
import contextlib
import functools
import getpass
import logging
import mimetypes
import os
import pprint
import random
import sys
import tempfile
import threading
import time
import traceback
import urlparse
import uuid
import errno

import babel.core
import simplejson
import werkzeug.contrib.sessions
import werkzeug.datastructures
import werkzeug.exceptions
import werkzeug.utils
import werkzeug.wrappers
import werkzeug.wsgi
import werkzeug.routing as routing
import urllib2

import openerp

import session

import inspect
import functools

_logger = logging.getLogger(__name__)

#----------------------------------------------------------
# RequestHandler
#----------------------------------------------------------
class WebRequest(object):
    """ Parent class for all OpenERP Web request types, mostly deals with
    initialization and setup of the request object (the dispatching itself has
    to be handled by the subclasses)

    :param request: a wrapped werkzeug Request object
    :type request: :class:`werkzeug.wrappers.BaseRequest`

    .. attribute:: httprequest

        the original :class:`werkzeug.wrappers.Request` object provided to the
        request

    .. attribute:: httpsession

        a :class:`~collections.Mapping` holding the HTTP session data for the
        current http session

    .. attribute:: params

        :class:`~collections.Mapping` of request parameters, not generally
        useful as they're provided directly to the handler method as keyword
        arguments

    .. attribute:: session_id

        opaque identifier for the :class:`session.OpenERPSession` instance of
        the current request

    .. attribute:: session

        :class:`~session.OpenERPSession` instance for the current request

    .. attribute:: context

        :class:`~collections.Mapping` of context values for the current request

    .. attribute:: debug

        ``bool``, indicates whether the debug mode is active on the client

    .. attribute:: db

        ``str``, the name of the database linked to the current request. Can be ``None``
        if the current request uses the @nodb decorator.

    .. attribute:: uid

        ``int``, the id of the user related to the current request. Can be ``None``
        if the current request uses the @nodb or the @noauth decorator.
    """
    def __init__(self, httprequest, func, auth_method="auth"):
        self.httprequest = httprequest
        self.httpresponse = None
        self.httpsession = httprequest.session
        self.db = None
        self.uid = None
        self.func = func
        self.auth_method = auth_method
        self._cr_cm = None
        self._cr = None

    def init(self, params):
        self.params = dict(params)
        # OpenERP session setup
        self.session_id = self.params.pop("session_id", None)
        if not self.session_id:
            i0 = self.httprequest.cookies.get("instance0|session_id", None)
            if i0:
                self.session_id = simplejson.loads(urllib2.unquote(i0))
            else:
                self.session_id = uuid.uuid4().hex
        self.session = self.httpsession.get(self.session_id)
        if not self.session:
            self.session = session.OpenERPSession()
            self.httpsession[self.session_id] = self.session

        # TODO: remove this shit
        # set db/uid trackers - they're cleaned up at the WSGI
        # dispatching phase in openerp.service.wsgi_server.application
        if self.session._db:
            threading.current_thread().dbname = self.session._db
        if self.session._uid:
            threading.current_thread().uid = self.session._uid

        self.context = self.params.pop('context', {})
        self.debug = self.params.pop('debug', False) is not False
        # Determine self.lang
        lang = self.params.get('lang', None)
        if lang is None:
            lang = self.context.get('lang')
        if lang is None:
            lang = self.httprequest.cookies.get('lang')
        if lang is None:
            lang = self.httprequest.accept_languages.best
        if not lang:
            lang = 'en_US'
        # tranform 2 letters lang like 'en' into 5 letters like 'en_US'
        lang = babel.core.LOCALE_ALIASES.get(lang, lang)
        # we use _ as seprator where RFC2616 uses '-'
        self.lang = lang.replace('-', '_')

    def _authenticate(self):
        if self.auth_method == "nodb":
            self.db = None
            self.uid = None
        elif self.auth_method == "noauth":
            self.db = (self.session._db or openerp.addons.web.controllers.main.db_monodb()).lower()
            if not self.db:
                raise session.SessionExpiredException("No valid database for request %s" % self.httprequest)
            self.uid = None
        else: # auth
            try:
                self.session.check_security()
            except session.SessionExpiredException, e:
                raise session.SessionExpiredException("Session expired for request %s" % self.httprequest)
            self.db = self.session._db
            self.uid = self.session._uid

    @property
    def registry(self):
        """
        The registry to the database linked to this request. Can be ``None`` if the current request uses the
        @nodb decorator.
        """
        return openerp.modules.registry.RegistryManager.get(self.db) if self.db else None

    @property
    def cr(self):
        """
        The cursor initialized for the current method call. If the current request uses the @nodb decorator
        trying to access this property will raise an exception.
        """
        # some magic to lazy create the cr
        if not self._cr_cm:
            self._cr_cm = self.registry.cursor()
            self._cr = self._cr_cm.__enter__()
        return self._cr

    def _call_function(self, *args, **kwargs):
        self._authenticate()
        try:
            # ugly syntax only to get the __exit__ arguments to pass to self._cr
            request = self
            class with_obj(object):
                def __enter__(self):
                    pass
                def __exit__(self, *args):
                    if request._cr_cm:
                        request._cr_cm.__exit__(*args)
                        request._cr_cm = None
                        request._cr = None

            with with_obj():
                return self.func(*args, **kwargs)
        finally:
            # just to be sure no one tries to re-use the request
            self.db = None
            self.uid = None


def noauth(f):
    """
    Decorator to put on a controller method to inform it does not require a user to be logged. When this decorator
    is used, ``request.uid`` will be ``None``. The request will still try to detect the database and an exception
    will be launched if there is no way to guess it.
    """
    f.auth = "noauth"
    return f

def nodb(f):
    """
    Decorator to put on a controller method to inform it does not require authentication nor any link to a database.
    When this decorator is used, ``request.uid`` and ``request.db`` will be ``None``. Trying to use ``request.cr``
    will launch an exception.
    """
    f.auth = "nodb"
    return f

def reject_nonliteral(dct):
    if '__ref' in dct:
        raise ValueError(
            "Non literal contexts can not be sent to the server anymore (%r)" % (dct,))
    return dct

class JsonRequest(WebRequest):
    """ JSON-RPC2 over HTTP.

    Sucessful request::

      --> {"jsonrpc": "2.0",
           "method": "call",
           "params": {"session_id": "SID",
                      "context": {},
                      "arg1": "val1" },
           "id": null}

      <-- {"jsonrpc": "2.0",
           "result": { "res1": "val1" },
           "id": null}

    Request producing a error::

      --> {"jsonrpc": "2.0",
           "method": "call",
           "params": {"session_id": "SID",
                      "context": {},
                      "arg1": "val1" },
           "id": null}

      <-- {"jsonrpc": "2.0",
           "error": {"code": 1,
                     "message": "End user error message.",
                     "data": {"code": "codestring",
                              "debug": "traceback" } },
           "id": null}

    """
    def dispatch(self):
        """ Calls the method asked for by the JSON-RPC2 or JSONP request

        :returns: an utf8 encoded JSON-RPC2 or JSONP reply
        """
        args = self.httprequest.args
        jsonp = args.get('jsonp')
        requestf = None
        request = None
        request_id = args.get('id')

        if jsonp and self.httprequest.method == 'POST':
            # jsonp 2 steps step1 POST: save call
            self.init(args)
            self.session.jsonp_requests[request_id] = self.httprequest.form['r']
            headers=[('Content-Type', 'text/plain; charset=utf-8')]
            r = werkzeug.wrappers.Response(request_id, headers=headers)
            return r
        elif jsonp and args.get('r'):
            # jsonp method GET
            request = args.get('r')
        elif jsonp and request_id:
            # jsonp 2 steps step2 GET: run and return result
            self.init(args)
            request = self.session.jsonp_requests.pop(request_id, "")
        else:
            # regular jsonrpc2
            requestf = self.httprequest.stream

        response = {"jsonrpc": "2.0" }
        error = None
        try:
            # Read POST content or POST Form Data named "request"
            if requestf:
                self.jsonrequest = simplejson.load(requestf, object_hook=reject_nonliteral)
            else:
                self.jsonrequest = simplejson.loads(request, object_hook=reject_nonliteral)
            self.init(self.jsonrequest.get("params", {}))
            #if _logger.isEnabledFor(logging.DEBUG):
            #    _logger.debug("--> %s.%s\n%s", func.im_class.__name__, func.__name__, pprint.pformat(self.jsonrequest))
            response['id'] = self.jsonrequest.get('id')
            response["result"] = self._call_function(**self.params)
        except session.AuthenticationError, e:
            _logger.exception("Exception during JSON request handling.")
            se = serialize_exception(e)
            error = {
                'code': 100,
                'message': "OpenERP Session Invalid",
                'data': se
            }
        except Exception, e:
            _logger.exception("Exception during JSON request handling.")
            se = serialize_exception(e)
            error = {
                'code': 200,
                'message': "OpenERP Server Error",
                'data': se
            }
        if error:
            response["error"] = error

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug("<--\n%s", pprint.pformat(response))

        if jsonp:
            # If we use jsonp, that's mean we are called from another host
            # Some browser (IE and Safari) do no allow third party cookies
            # We need then to manage http sessions manually.
            response['httpsessionid'] = self.httpsession.sid
            mime = 'application/javascript'
            body = "%s(%s);" % (jsonp, simplejson.dumps(response),)
        else:
            mime = 'application/json'
            body = simplejson.dumps(response)

        r = werkzeug.wrappers.Response(body, headers=[('Content-Type', mime), ('Content-Length', len(body))])
        return r

def serialize_exception(e):
    tmp = {
        "name": type(e).__module__ + "." + type(e).__name__ if type(e).__module__ else type(e).__name__,
        "debug": traceback.format_exc(),
        "message": u"%s" % e,
        "arguments": to_jsonable(e.args),
    }
    if isinstance(e, openerp.osv.osv.except_osv):
        tmp["exception_type"] = "except_osv"
    elif isinstance(e, openerp.exceptions.Warning):
        tmp["exception_type"] = "warning"
    elif isinstance(e, openerp.exceptions.AccessError):
        tmp["exception_type"] = "access_error"
    elif isinstance(e, openerp.exceptions.AccessDenied):
        tmp["exception_type"] = "access_denied"
    return tmp

def to_jsonable(o):
    if isinstance(o, str) or isinstance(o,unicode) or isinstance(o, int) or isinstance(o, long) \
        or isinstance(o, bool) or o is None or isinstance(o, float):
        return o
    if isinstance(o, list) or isinstance(o, tuple):
        return [to_jsonable(x) for x in o]
    if isinstance(o, dict):
        tmp = {}
        for k, v in o.items():
            tmp[u"%s" % k] = to_jsonable(v)
        return tmp
    return u"%s" % o

def jsonrequest(f):
    """ Decorator marking the decorated method as being a handler for a
    JSON-RPC request (the exact request path is specified via the
    ``$(Controller._cp_path)/$methodname`` combination.

    If the method is called, it will be provided with a :class:`JsonRequest`
    instance and all ``params`` sent during the JSON-RPC request, apart from
    the ``session_id``, ``context`` and ``debug`` keys (which are stripped out
    beforehand)
    """
    f.exposed = 'json'
    return f

class HttpRequest(WebRequest):
    """ Regular GET/POST request
    """
    def dispatch(self):
        params = dict(self.httprequest.args)
        params.update(self.httprequest.form)
        params.update(self.httprequest.files)
        self.init(params)
        akw = {}
        for key, value in self.httprequest.args.iteritems():
            if isinstance(value, basestring) and len(value) < 1024:
                akw[key] = value
            else:
                akw[key] = type(value)
        #_logger.debug("%s --> %s.%s %r", self.httprequest.func, func.im_class.__name__, func.__name__, akw)
        try:
            r = self._call_function(**self.params)
        except werkzeug.exceptions.HTTPException, e:
            r = e
        except Exception, e:
            _logger.exception("An exception occured during an http request")
            se = serialize_exception(e)
            error = {
                'code': 200,
                'message': "OpenERP Server Error",
                'data': se
            }
            r = werkzeug.exceptions.InternalServerError(cgi.escape(simplejson.dumps(error)))
        else:
            if not r:
                r = werkzeug.wrappers.Response(status=204)  # no content
        if isinstance(r, (werkzeug.wrappers.BaseResponse, werkzeug.exceptions.HTTPException)):
            _logger.debug('<-- %s', r)
        else:
            _logger.debug("<-- size: %s", len(r))
        return r

    def make_response(self, data, headers=None, cookies=None):
        """ Helper for non-HTML responses, or HTML responses with custom
        response headers or cookies.

        While handlers can just return the HTML markup of a page they want to
        send as a string if non-HTML data is returned they need to create a
        complete response object, or the returned data will not be correctly
        interpreted by the clients.

        :param basestring data: response body
        :param headers: HTTP headers to set on the response
        :type headers: ``[(name, value)]``
        :param collections.Mapping cookies: cookies to set on the client
        """
        response = werkzeug.wrappers.Response(data, headers=headers)
        if cookies:
            for k, v in cookies.iteritems():
                response.set_cookie(k, v)
        return response

    def not_found(self, description=None):
        """ Helper for 404 response, return its result from the method
        """
        return werkzeug.exceptions.NotFound(description)

def httprequest(f):
    """ Decorator marking the decorated method as being a handler for a
    normal HTTP request (the exact request path is specified via the
    ``$(Controller._cp_path)/$methodname`` combination.

    If the method is called, it will be provided with a :class:`HttpRequest`
    instance and all ``params`` sent during the request (``GET`` and ``POST``
    merged in the same dictionary), apart from the ``session_id``, ``context``
    and ``debug`` keys (which are stripped out beforehand)
    """
    f.exposed = 'http'
    return f

#----------------------------------------------------------
# Local storage of requests
#----------------------------------------------------------
from werkzeug.local import LocalStack

_request_stack = LocalStack()

def set_request(request):
    class with_obj(object):
        def __enter__(self):
            _request_stack.push(request)
        def __exit__(self, *args):
            _request_stack.pop()
    return with_obj()

"""
    A global proxy that always redirect to the current request object.
"""
request = _request_stack()

#----------------------------------------------------------
# Controller registration with a metaclass
#----------------------------------------------------------
addons_module = {}
addons_manifest = {}
controllers_class_path = {}
controllers_object = {}
controllers_object_path = {}
controllers_path = {}

class ControllerType(type):
    def __init__(cls, name, bases, attrs):
        super(ControllerType, cls).__init__(name, bases, attrs)

        # create wrappers for old-style methods with req as first argument
        cls._methods_wrapper = {}
        for k, v in attrs.items():
            if inspect.isfunction(v):
                spec = inspect.getargspec(v)
                first_arg = spec.args[1] if len(spec.args) >= 2 else None
                if first_arg in ["req", "request"]:
                    def build_new(nv):
                        return lambda self, *args, **kwargs: nv(self, request, *args, **kwargs)
                    cls._methods_wrapper[k] = build_new(v)

        # store the controller in the controllers list
        name_class = ("%s.%s" % (cls.__module__, cls.__name__), cls)
        path = attrs.get('_cp_path')
        if path and path not in controllers_class_path:
            controllers_class_path[path] = name_class

class Controller(object):
    __metaclass__ = ControllerType

    def __new__(cls, *args, **kwargs):
        subclasses = [c for c in cls.__subclasses__() if c._cp_path == cls._cp_path]
        if subclasses:
            name = "%s (extended by %s)" % (cls.__name__, ', '.join(sub.__name__ for sub in subclasses))
            cls = type(name, tuple(reversed(subclasses)), {})

        return object.__new__(cls)

    def get_wrapped_method(self, name):
        if name in self.__class__._methods_wrapper:
            return functools.partial(self.__class__._methods_wrapper[name], self)
        else:
            return getattr(self, name)

#----------------------------------------------------------
# Session context manager
#----------------------------------------------------------
@contextlib.contextmanager
def session_context(request, session_store, session_lock, sid):
    with session_lock:
        if sid:
            request.session = session_store.get(sid)
        else:
            request.session = session_store.new()
    try:
        yield request.session
    finally:
        # Remove all OpenERPSession instances with no uid, they're generated
        # either by login process or by HTTP requests without an OpenERP
        # session id, and are generally noise
        removed_sessions = set()
        for key, value in request.session.items():
            if not isinstance(value, session.OpenERPSession):
                continue
            if getattr(value, '_suicide', False) or (
                        not value._uid
                    and not value.jsonp_requests
                    # FIXME do not use a fixed value
                    and value._creation_time + (60*5) < time.time()):
                _logger.debug('remove session %s', key)
                removed_sessions.add(key)
                del request.session[key]

        with session_lock:
            if sid:
                # Re-load sessions from storage and merge non-literal
                # contexts and domains (they're indexed by hash of the
                # content so conflicts should auto-resolve), otherwise if
                # two requests alter those concurrently the last to finish
                # will overwrite the previous one, leading to loss of data
                # (a non-literal is lost even though it was sent to the
                # client and client errors)
                #
                # note that domains_store and contexts_store are append-only (we
                # only ever add items to them), so we can just update one with the
                # other to get the right result, if we want to merge the
                # ``context`` dict we'll need something smarter
                in_store = session_store.get(sid)
                for k, v in request.session.iteritems():
                    stored = in_store.get(k)
                    if stored and isinstance(v, session.OpenERPSession):
                        if hasattr(v, 'contexts_store'):
                            del v.contexts_store
                        if hasattr(v, 'domains_store'):
                            del v.domains_store
                        if not hasattr(v, 'jsonp_requests'):
                            v.jsonp_requests = {}
                        v.jsonp_requests.update(getattr(
                            stored, 'jsonp_requests', {}))

                # add missing keys
                for k, v in in_store.iteritems():
                    if k not in request.session and k not in removed_sessions:
                        request.session[k] = v

            session_store.save(request.session)

def session_gc(session_store):
    if random.random() < 0.001:
        # we keep session one week
        last_week = time.time() - 60*60*24*7
        for fname in os.listdir(session_store.path):
            path = os.path.join(session_store.path, fname)
            try:
                if os.path.getmtime(path) < last_week:
                    os.unlink(path)
            except OSError:
                pass

#----------------------------------------------------------
# WSGI Application
#----------------------------------------------------------
# Add potentially missing (older ubuntu) font mime types
mimetypes.add_type('application/font-woff', '.woff')
mimetypes.add_type('application/vnd.ms-fontobject', '.eot')
mimetypes.add_type('application/x-font-ttf', '.ttf')

class DisableCacheMiddleware(object):
    def __init__(self, app):
        self.app = app
    def __call__(self, environ, start_response):
        def start_wrapped(status, headers):
            referer = environ.get('HTTP_REFERER', '')
            parsed = urlparse.urlparse(referer)
            debug = parsed.query.count('debug') >= 1

            new_headers = []
            unwanted_keys = ['Last-Modified']
            if debug:
                new_headers = [('Cache-Control', 'no-cache')]
                unwanted_keys += ['Expires', 'Etag', 'Cache-Control']

            for k, v in headers:
                if k not in unwanted_keys:
                    new_headers.append((k, v))

            start_response(status, new_headers)
        return self.app(environ, start_wrapped)

def session_path():
    try:
        username = getpass.getuser()
    except Exception:
        username = "unknown"
    path = os.path.join(tempfile.gettempdir(), "oe-sessions-" + username)
    try:
        os.mkdir(path, 0700)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            # directory exists: ensure it has the correct permissions
            # this will fail if the directory is not owned by the current user
            os.chmod(path, 0700)
        else:
            raise
    return path

class Root(object):
    """Root WSGI application for the OpenERP Web Client.
    """
    def __init__(self):
        self.addons = {}
        self.statics = {}
        self.routing_map = None

        self.load_addons()

        # Setup http sessions
        path = session_path()
        self.session_store = werkzeug.contrib.sessions.FilesystemSessionStore(path)
        self.session_lock = threading.Lock()
        _logger.debug('HTTP sessions stored in: %s', path)

    def __call__(self, environ, start_response):
        """ Handle a WSGI request
        """
        return self.dispatch(environ, start_response)

    def dispatch(self, environ, start_response):
        """
        Performs the actual WSGI dispatching for the application, may be
        wrapped during the initialization of the object.

        Call the object directly.
        """
        request = werkzeug.wrappers.Request(environ)
        request.parameter_storage_class = werkzeug.datastructures.ImmutableDict
        request.app = self

        handler = self.find_handler(*(request.path.split('/')[1:]))

        if not handler:
            response = werkzeug.exceptions.NotFound()
        else:
            sid = request.cookies.get('sid')
            if not sid:
                sid = request.args.get('sid')

            session_gc(self.session_store)

            with session_context(request, self.session_store, self.session_lock, sid) as session:
                result = handler(request)

                if isinstance(result, basestring):
                    headers=[('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', len(result))]
                    response = werkzeug.wrappers.Response(result, headers=headers)
                else:
                    response = result

                if hasattr(response, 'set_cookie'):
                    response.set_cookie('sid', session.sid)

        return response(environ, start_response)

    def load_addons(self):
        """ Load all addons from addons patch containg static files and
        controllers and configure them.  """

        for addons_path in openerp.modules.module.ad_paths:
            for module in sorted(os.listdir(str(addons_path))):
                if module not in addons_module:
                    manifest_path = os.path.join(addons_path, module, '__openerp__.py')
                    path_static = os.path.join(addons_path, module, 'static')
                    if os.path.isfile(manifest_path) and os.path.isdir(path_static):
                        manifest = ast.literal_eval(open(manifest_path).read())
                        manifest['addons_path'] = addons_path
                        _logger.debug("Loading %s", module)
                        if 'openerp.addons' in sys.modules:
                            m = __import__('openerp.addons.' + module)
                        else:
                            m = __import__(module)
                        addons_module[module] = m
                        addons_manifest[module] = manifest
                        self.statics['/%s/static' % module] = path_static

        for k, v in controllers_class_path.items():
            o = v[1]()
            controllers_object[v[0]] = o
            controllers_object_path[k] = o
            if hasattr(o, '_cp_path'):
                controllers_path[o._cp_path] = o

        app = werkzeug.wsgi.SharedDataMiddleware(self.dispatch, self.statics)
        self.dispatch = DisableCacheMiddleware(app)

    def find_handler(self, *l):
        """
        Tries to discover the controller handling the request for the path
        specified by the provided parameters

        :param l: path sections to a controller or controller method
        :returns: a callable matching the path sections, or ``None``
        :rtype: ``Controller | None``
        """
        if l:
            ps = '/' + '/'.join(filter(None, l))
            method_name = 'index'
            while ps:
                c = controllers_path.get(ps)
                if c:
                    method = getattr(c, method_name, None)
                    if method:
                        exposed = getattr(method, 'exposed', False)
                        auth = getattr(method, 'auth', "auth")
                        method = c.get_wrapped_method(method_name)
                        if exposed == 'json':
                            _logger.debug("Dispatch json to %s %s %s", ps, c, method_name)
                            def fct(_request):
                                _req = JsonRequest(_request, method, auth)
                                with set_request(_req):
                                    return request.dispatch()
                            return fct
                        elif exposed == 'http':
                            _logger.debug("Dispatch http to %s %s %s", ps, c, method_name)
                            def fct(_request):
                                _req = HttpRequest(_request, method, auth)
                                with set_request(_req):
                                    return request.dispatch()
                            return fct
                    if method_name != "index":
                        method_name = "index"
                        continue
                ps, _slash, method_name = ps.rpartition('/')
                if not ps and method_name:
                    ps = '/'
        return None

def wsgi_postload():
    openerp.wsgi.register_wsgi_handler(Root())

# vim:et:ts=4:sw=4:
