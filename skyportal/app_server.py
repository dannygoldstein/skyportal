import tornado.web

from baselayer.app.app_server import MainPageHandler

from skyportal.handlers import (SourceHandler, CommentHandler, GroupHandler,
                                GroupUserHandler, PlotPhotometryHandler,
                                PlotSpectroscopyHandler, ProfileHandler,
                                BecomeUserHandler, LogoutHandler,
                                PhotometryHandler, TokenHandler, SourcePhotometryHandler,
                                FilterSourcesHandler, SysInfoHandler,
                                UserHandler, SpectrumHandler, ThumbnailHandler,
                                DBInfoHandler, TelescopeHandler, InstrumentHandler)
from skyportal import models, model_util, openapi


def make_app(cfg, baselayer_handlers, baselayer_settings):
    """Create and return a `tornado.web.Application` object with specified
    handlers and settings.

    Parameters
    ----------
    cfg : Config
        Loaded configuration.  Can be specified with '--config'
        (multiple uses allowed).
    baselayer_handlers : list
        Tornado handlers needed for baselayer to function.
    baselayer_settings : cfg
        Settings needed for baselayer to function.

    """
    if cfg['cookie_secret'] == 'abc01234':
        print('!' * 80)
        print('  Your server is insecure. Please update the secret string ')
        print('  in the configuration file!')
        print('!' * 80)

    handlers = baselayer_handlers + [
        # API endpoints
        (r'/api/sources(/[0-9A-Za-z-]+)/photometry', SourcePhotometryHandler),
        (r'/api/sources/filter', FilterSourcesHandler),
        (r'/api/sources(/.*)?', SourceHandler),
        (r'/api/groups/(.*)/users/(.*)?', GroupUserHandler),
        (r'/api/groups(/.*)?', GroupHandler),
        (r'/api/comment(/[0-9]+)?', CommentHandler),
        (r'/api/comment(/[0-9]+)/(download_attachment)', CommentHandler),
        (r'/api/photometry(/[0-9]+)?', PhotometryHandler),
        (r'/api/spectrum(/[0-9]+)?', SpectrumHandler),
        (r'/api/telescope(/[0-9]+)?', TelescopeHandler),
        (r'/api/instrument(/[0-9]+)?', InstrumentHandler),
        (r'/api/thumbnail(/[0-9]+)?', ThumbnailHandler),
        (r'/api/user(/.*)?', UserHandler),
        (r'/api/sysinfo', SysInfoHandler),

        (r'/api/internal/tokens(/.*)?', TokenHandler),
        (r'/api/internal/profile', ProfileHandler),
        (r'/api/internal/dbinfo', DBInfoHandler),
        (r'/api/internal/plot/photometry/(.*)', PlotPhotometryHandler),
        (r'/api/internal/plot/spectroscopy/(.*)', PlotSpectroscopyHandler),

        (r'/become_user(/.*)?', BecomeUserHandler),
        (r'/logout', LogoutHandler),

        # User-facing pages
        (r'/.*', MainPageHandler)  # Route all frontend pages, such as
                                   # `/source/g647ba`, through the main page.
                                   #
                                   # Refer to Main.jsx for routing info.
    ]

    settings = baselayer_settings
    settings.update({})  # Specify any additional settings here

    app = tornado.web.Application(handlers, **settings)
    models.init_db(**cfg['database'])
    model_util.create_tables()
    model_util.setup_permissions()
    app.cfg = cfg

    app.openapi_spec = openapi.spec_from_handlers(handlers)

    return app
