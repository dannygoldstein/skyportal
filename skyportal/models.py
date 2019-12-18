import os.path
import re
import requests
import numpy as np

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as psql
from sqlalchemy.orm import backref, relationship, mapper
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy_utils import ArrowType
from sqlalchemy import Index
from sqlalchemy import func

from baselayer.app.models import (init_db, join_model, Base, DBSession, ACL,
                                  Role, User, Token)

from . import schema


def is_owned_by(self, user_or_token):
    """Generic ownership logic for any `skyportal` ORM model.

    Models with complicated ownership logic should implement their own method
    instead of adding too many additional conditions here.
    """
    if hasattr(self, 'tokens'):
        return (user_or_token in self.tokens)
    elif hasattr(self, 'groups'):
        return bool(set(self.groups) & set(user_or_token.groups))
    elif hasattr(self, 'users'):
        return (user_or_token in self.users)
    else:
        raise NotImplementedError(f"{type(self).__name__} object has no owner")
Base.is_owned_by = is_owned_by


class NumpyArray(sa.types.TypeDecorator):
    impl = psql.ARRAY(sa.Float)

    def process_result_value(self, value, dialect):
        return np.array(value)


class Group(Base):
    name = sa.Column(sa.String, unique=True, nullable=False)

    sources = relationship('Source', secondary='group_sources', cascade='all')
    streams = relationship('Stream', secondary='stream_groups', cascade='all',
                           back_populates='groups')
    group_users = relationship('GroupUser', back_populates='group',
                               cascade='all', passive_deletes=True)
    users = relationship('User', secondary='group_users', cascade='all',
                         back_populates='groups')
    group_tokens = relationship('GroupToken', back_populates='group',
                               cascade='all', passive_deletes=True)
    tokens = relationship('Token', secondary='group_tokens', cascade='all',
                          back_populates='groups')


GroupToken = join_model('group_tokens', Group, Token)
Token.groups = relationship('Group', secondary='group_tokens', cascade='all',
                            back_populates='tokens')
Token.group_tokens = relationship('GroupToken', back_populates='token', cascade='all')

GroupUser = join_model('group_users', Group, User)
GroupUser.admin = sa.Column(sa.Boolean, nullable=False, default=False)


class Stream(Base):
    name = sa.Column(sa.String, unique=True, nullable=False)
    url = sa.Column(sa.String, unique=True, nullable=False)
    username = sa.Column(sa.String)
    password = sa.Column(sa.String)

    groups = relationship('Group', secondary='stream_groups', cascade='all',
                          back_populates='streams')


StreamGroup = join_model('stream_groups', Stream, Group)


User.group_users = relationship('GroupUser', back_populates='user', cascade='all')
User.groups = relationship('Group', secondary='group_users', cascade='all',
                           back_populates='users')


class Source(Base):
    id = sa.Column(sa.String, primary_key=True)
    # TODO should this column type be decimal? fixed-precison numeric
    ra = sa.Column(sa.Float)
    dec = sa.Column(sa.Float)

    ra_dis = sa.Column(sa.Float)
    dec_dis = sa.Column(sa.Float)

    ra_err = sa.Column(sa.Float, nullable=True)
    dec_err = sa.Column(sa.Float, nullable=True)

    offset = sa.Column(sa.Float, default=0.0)
    redshift = sa.Column(sa.Float, nullable=True)

    altdata = sa.Column(JSONB, nullable=True)
    created = sa.Column(ArrowType, nullable=False,
                        server_default=sa.func.now())

    last_detected = sa.Column(ArrowType, nullable=True)
    dist_nearest_source = sa.Column(sa.Float, nullable=True)
    mag_nearest_source = sa.Column(sa.Float, nullable=True)
    e_mag_nearest_source = sa.Column(sa.Float, nullable=True)

    transient = sa.Column(sa.Boolean, default=False)
    varstar = sa.Column(sa.Boolean, default=False)
    is_roid = sa.Column(sa.Boolean, default=False)

    score = sa.Column(sa.Float, nullable=True)

    ## pan-starrs
    sgmag1 = sa.Column(sa.Float, nullable=True)
    srmag1 = sa.Column(sa.Float, nullable=True)
    simag1 = sa.Column(sa.Float, nullable=True)
    objectidps1 = sa.Column(sa.BigInteger, nullable=True)
    sgscore1 = sa.Column(sa.Float, nullable=True)
    distpsnr1 = sa.Column(sa.Float, nullable=True)

    origin = sa.Column(sa.String, nullable=True)
    modified = sa.Column(sa.DateTime, nullable=False,
                         server_default=sa.func.now(),
                         server_onupdate=sa.func.now())

    simbad_class = sa.Column(sa.Unicode, nullable=True, )
    simbad_info = sa.Column(JSONB, nullable=True)
    gaia_info = sa.Column(JSONB, nullable=True)
    tns_info = sa.Column(JSONB, nullable=True)
    tns_name = sa.Column(sa.Unicode, nullable=True)

    groups = relationship('Group', secondary='group_sources', cascade='all')
    comments = relationship('Comment', back_populates='source', cascade='all',
                            order_by="Comment.created_at")
    photometry = relationship('Photometry', back_populates='source',
                              cascade='all',
                              order_by="Photometry.observed_at")

    detect_photometry_count = sa.Column(sa.Integer, nullable=True)

    spectra = relationship('Spectrum', back_populates='source', cascade='all',
                           order_by="Spectrum.observed_at")
    thumbnails = relationship('Thumbnail', back_populates='source',
                              secondary='photometry', cascade='all')

    def add_linked_thumbnails(self, commit=True):

        to_add = []
        thumbtypes = [t.type for t in self.thumbnails]

        if len(self.photometry) == 0:
            return

        if 'sdss' not in thumbtypes:
            sdss_thumb = Thumbnail(photometry=self.photometry[0],
                                   public_url=self.get_sdss_url(),
                                   type='sdss')
            to_add.append(sdss_thumb)

        if 'ps1' not in thumbtypes:
            ps1_thumb = Thumbnail(photometry=self.photometry[0],
                                  public_url=self.get_panstarrs_url(),
                                  type='ps1')
            to_add.append(ps1_thumb)

        if 'lsdr8-model' not in thumbtypes:
            ls_thumb = Thumbnail(photometry=self.photometry[0],
                                 public_url=self.get_decals_url(),
                                 type='lsdr8-model')
            to_add.append(ls_thumb)

        DBSession().add_all(to_add)

        if commit:
            DBSession().commit()

    def get_decals_url(self, layer='dr8'):
        return (f"http://legacysurvey.org//viewer/cutout.jpg?ra={self.ra}"
                f"&dec={self.dec}&zoom=15&layer={layer}")

    def get_sdss_url(self):
        """Construct URL for public Sloan Digital Sky Survey (SDSS) cutout."""
        return (f"http://skyservice.pha.jhu.edu/DR9/ImgCutout/getjpeg.aspx"
                f"?ra={self.ra}&dec={self.dec}&scale=0.3&width=200&height=200"
                f"&opt=G&query=&Grid=on")

    def get_panstarrs_url(self):
        """Construct URL for public PanSTARRS-1 (PS1) cutout.

        The cutout service doesn't allow directly querying for an image; the
        best we can do is request a page that contains a link to the image we
        want (in this case a combination of the green/blue/red filters).
        """
        try:
            ps_query_url = (f"http://ps1images.stsci.edu/cgi-bin/ps1cutouts"
                            f"?pos={self.ra}+{self.dec}&filter=color&filter=g"
                            f"&filter=r&filter=i&filetypes=stack&size=250")
            response = requests.get(ps_query_url)
            match = re.search('src="//ps1images.stsci.edu.*?"', response.content.decode())
            return match.group().replace('src="', 'http:').replace('"', '')
        except (ValueError, ConnectionError) as e:
            return None

    q3c = Index('q3c_ang2ipix_sources_idx', func.q3c_ang2ipix(ra, dec))




GroupSource = join_model('group_sources', Group, Source)
"""User.sources defines the logic for whether a user has access to a source;
   if this gets more complicated it should become a function/`hybrid_property`
   rather than a `relationship`.
"""
User.sources = relationship('Source', backref='users',
                            secondary='join(Group, group_sources).join(group_users)',
                            primaryjoin='group_users.c.user_id == users.c.id')


class Telescope(Base):
    name = sa.Column(sa.String, nullable=False)
    nickname = sa.Column(sa.String, nullable=False)
    lat = sa.Column(sa.Float, nullable=False)
    lon = sa.Column(sa.Float, nullable=False)
    elevation = sa.Column(sa.Float, nullable=False)
    diameter = sa.Column(sa.Float, nullable=False)

    instruments = relationship('Instrument', back_populates='telescope',
                               cascade='all')


class Instrument(Base):
    name = sa.Column(sa.String, nullable=False)
    type = sa.Column(sa.String, nullable=False)
    band = sa.Column(sa.String, nullable=False)

    telescope_id = sa.Column(sa.ForeignKey('telescopes.id',
                                           ondelete='CASCADE'),
                             nullable=False, index=True)
    telescope = relationship('Telescope', back_populates='instruments',
                             cascade='all')
    photometry = relationship('Photometry', back_populates='instrument',
                              cascade='all')
    spectra = relationship('Spectrum', back_populates='instrument',
                           cascade='all')


class Comment(Base):
    text = sa.Column(sa.String, nullable=False)
    ctype = sa.Column(sa.Enum('text', 'redshift', 'classification',
                             name='comment_types', validate_strings=True))

    attachment_name = sa.Column(sa.String, nullable=True)
    attachment_type = sa.Column(sa.String, nullable=True)
    attachment_bytes = sa.Column(sa.types.LargeBinary, nullable=True)

    origin = sa.Column(sa.String, nullable=True)
    author = sa.Column(sa.String, nullable=False)
    source_id = sa.Column(sa.ForeignKey('sources.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    source = relationship('Source', back_populates='comments', cascade='all')


class Photometry(Base):
    __tablename__ = 'photometry'
    observed_at = sa.Column(ArrowType) # iso date
    mjd = sa.Column(sa.Float)  # mjd date
    time_format = sa.Column(sa.String, default='iso')
    time_scale = sa.Column(sa.String, default='utc')

    flux = sa.Column(sa.Float)
    fluxerr = sa.Column(sa.Float)

    zp = sa.Column(sa.Float)
    zpsys = sa.Column(sa.Text) # should be enum

    lim_mag = sa.Column(sa.Float)

    ra = sa.Column(sa.Float)
    dec = sa.Column(sa.Float)

    filter = sa.Column(sa.String)  # TODO Enum?
    isdiffpos = sa.Column(sa.Boolean, default=True)  # candidate from position?

    var_mag = sa.Column(sa.Float, nullable=True)
    var_e_mag = sa.Column(sa.Float, nullable=True)

    dist_nearest_source = sa.Column(sa.Float, nullable=True)
    mag_nearest_source = sa.Column(sa.Float, nullable=True)
    e_mag_nearest_source = sa.Column(sa.Float, nullable=True)

    ## external values
    score = sa.Column(sa.Float, nullable=True)  # RB
    candid = sa.Column(sa.BigInteger, nullable=True)  # candidate ID
    altdata = sa.Column(JSONB)

    created = sa.Column(sa.DateTime, nullable=False,
                        server_default=sa.func.now())

    origin = sa.Column(sa.String, nullable=True)

    source_id = sa.Column(sa.ForeignKey('sources.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    source = relationship('Source', back_populates='photometry', cascade='all')
    instrument_id = sa.Column(sa.ForeignKey('instruments.id',
                                            ondelete='CASCADE'),
                              nullable=False, index=True)
    instrument = relationship('Instrument', back_populates='photometry',
                              cascade='all')
    thumbnails = relationship('Thumbnail', cascade='all')
    q3c = Index('q3c_ang2ipix_photometry_idx', func.q3c_ang2ipix(ra, dec))


class Spectrum(Base):
    __tablename__ = 'spectra'
    # TODO better numpy integration
    wavelengths = sa.Column(NumpyArray, nullable=False)
    fluxes = sa.Column(NumpyArray, nullable=False)
    errors = sa.Column(NumpyArray)

    source_id = sa.Column(sa.ForeignKey('sources.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    source = relationship('Source', back_populates='spectra', cascade='all')
    observed_at = sa.Column(sa.DateTime, nullable=False)
    origin = sa.Column(sa.String, nullable=True)
    # TODO program?
    instrument_id = sa.Column(sa.ForeignKey('instruments.id',
                                            ondelete='CASCADE'),
                              nullable=False, index=True)
    instrument = relationship('Instrument', back_populates='spectra',
                              cascade='all')

    @classmethod
    def from_ascii(cls, filename, source_id, instrument_id, observed_at):
        data = np.loadtxt(filename)
        if data.shape[1] != 2:  # TODO support other formats
            raise ValueError(f"Expected 2 columns, got {data.shape[1]}")

        return cls(wavelengths=data[:, 0], fluxes=data[:, 1],
                   source_id=source_id, instrument_id=instrument_id,
                   observed_at=observed_at)


#def format_public_url(context):
#    """TODO migrate this to broker tools"""
#    file_uri = context.current_parameters.get('file_uri')
#    if file_uri is None:
#        return None
#    elif file_uri.startswith('s3'):  # TODO is this reliable?
#        raise NotImplementedError
#    elif file_uri.startswith('http://'): # TODO is this reliable?
#        return file_uri
#    else:  # local file
#        return '/' + file_uri.lstrip('./')


class Thumbnail(Base):
    # TODO delete file after deleting row
    type = sa.Column(sa.Enum('new', 'ref', 'sub', 'sdss', 'ps1', 'dr8', 'dr8-model'),
                     name='thumbnail_types', validate_strings=True))
    file_uri = sa.Column(sa.String(), nullable=True, index=False, unique=False)
    public_url = sa.Column(sa.String(), nullable=True, index=False, unique=False)
    origin = sa.Column(sa.String, nullable=True)
    photometry_id = sa.Column(sa.ForeignKey('photometry.id', ondelete='CASCADE'),
                              nullable=False, index=True)
    photometry = relationship('Photometry', back_populates='thumbnails', cascade='all')
    source = relationship('Source', back_populates='thumbnails', uselist=False,
                          secondary='photometry', cascade='all')


schema.setup_schema()
