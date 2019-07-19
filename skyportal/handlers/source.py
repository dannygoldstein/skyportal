import tornado.web
from sqlalchemy.orm import joinedload
from sqlalchemy import func
import arrow
from functools import reduce
from marshmallow.exceptions import ValidationError
from baselayer.app.access import permissions, auth_or_token
from .base import BaseHandler
from ..models import (DBSession, Comment, Instrument, Photometry, Source,
                      Thumbnail, GroupSource, Token, User)


SOURCES_PER_PAGE = 100


class SourceHandler(BaseHandler):
    @auth_or_token
    def get(self, source_id=None):
        """
        ---
        single:
          description: Retrieve a source
          parameters:
            - in: path
              name: source_id
              required: false
              schema:
                type: integer
          responses:
            200:
              content:
                application/json:
                  schema: SingleSource
            400:
              content:
                application/json:
                  schema: Error
        multiple:
          description: Retrieve all sources
          parameters:
            - in: query
              name: page
              schema:
                type: integer
              description: Queries are limited to 100 per page. This selects the page to download.
          responses:
            200:
              content:
               application/json:
                  schema: ArrayOfSources
            400:
              content:
                application/json:
                  schema: Error
        """
        info = {}
        page_number = self.get_query_argument('page', None)
        if source_id:
            info['sources'] = Source.get_if_owned_by(
                source_id, self.current_user,
                options=[joinedload(Source.comments),
                         joinedload(Source.thumbnails)
                         .joinedload(Thumbnail.photometry)
                         .joinedload(Photometry.instrument)
                         .joinedload(Instrument.telescope)])
        elif page_number:
            page = int(page_number)
            q = Source.query.filter(Source.id.in_(DBSession.query(
                GroupSource.source_id).filter(GroupSource.group_id.in_(
                    [g.id for g in self.current_user.groups]))))
            all_matches = q.all()
            info['totalMatches'] = len(all_matches)
            info['sources'] = all_matches[
                ((page - 1) * SOURCES_PER_PAGE):(page * SOURCES_PER_PAGE)]
            info['pageNumber'] = page
            info['sourceNumberingStart'] = (page - 1) * SOURCES_PER_PAGE + 1
            info['sourceNumberingEnd'] = min(info['totalMatches'],
                                             page * SOURCES_PER_PAGE)
            info['lastPage'] = info['totalMatches'] <= page * SOURCES_PER_PAGE
            if info['totalMatches'] == 0:
                info['sourceNumberingStart'] = 0
        else:
            if isinstance(self.current_user, Token):
                token = self.current_user
                info['sources'] = list(reduce(
                    set.union, (set(group.sources) for group in token.groups)))
            else:
                info['sources'] = self.current_user.sources

        if info['sources'] is not None:
            return self.success(data=info)
        else:
            return self.error(f"Could not load source {source_id}",
                              data={"source_id": source_id_or_page_num})

    @permissions(['Manage sources'])
    def post(self):
        """
        ---
        description: Upload a source
        parameters:
          - in: path
            name: source
            schema: Source
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - Success
                    - type: object
                      properties:
                        id:
                          type: integer
                          description: New source ID
        """
        data = self.get_json()

        s = Source(ra=data['sourceRA'], dec=data['sourceDec'],
                   redshift=data.get('redshift'))
        DBSession().add(s)
        DBSession().commit()

        return self.success(data={"id": s.id}, action='skyportal/FETCH_SOURCES')

    @permissions(['Manage sources'])
    def put(self, source_id):
        """
        ---
        description: Update a source
        parameters:
          - in: path
            name: source
            schema: Source
        responses:
          200:
            content:
              application/json:
                schema: Success
          400:
            content:
              application/json:
                schema: Error
        """
        data = self.get_json()
        data['id'] = source_id

        schema = Source.__schema__()
        try:
            schema.load(data)
        except ValidationError as e:
            return self.error('Invalid/missing parameters: '
                              f'{e.normalized_messages()}')
        DBSession().commit()

        return self.success(action='skyportal/REFRESH_SOURCE',
                            payload={'source_id': source_id})

    @permissions(['Manage sources'])
    def delete(self, source_id):
        """
        ---
        description: Delete a source
        parameters:
          - in: path
            name: source
            schema:
              Source
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        s = Source.query.get(source_id)
        DBSession().delete(s)
        DBSession().commit()

        return self.success(action='skyportal/FETCH_SOURCES')


class FilterSourcesHandler(BaseHandler):
    @auth_or_token
    def post(self):
        data = self.get_json()
        info = {}
        page = int(data.get('pageNumber', 1))
        info['pageNumber'] = page
        q = Source.query.filter(Source.id.in_(DBSession.query(
                GroupSource.source_id).filter(GroupSource.group_id.in_(
                    [g.id for g in self.current_user.groups]))))

        if data['sourceID']:
            q = q.filter(Source.id.contains(data['sourceID'].strip()))
        if data['ra'] and data['dec'] and data['radius']:
            ra = float(data['ra'])
            dec = float(data['dec'])
            radius = float(data['radius'])
            q = q.filter(Source.ra <= ra + radius)\
                 .filter(Source.ra >= ra - radius)\
                 .filter(Source.dec <= dec + radius)\
                 .filter(Source.dec >= dec - radius)
        if data['startDate']:
            start_date = arrow.get(data['startDate'].strip())
            q = q.filter(Source.last_detected >= start_date)
        if data['endDate']:
            end_date = arrow.get(data['endDate'].strip())
            q = q.filter(Source.last_detected <= end_date)
        if data['simbadClass']:
            q = q.filter(func.lower(Source.simbad_class) ==
                         data['simbadClass'].lower())
        if data['hasTNSname']:
            q = q.filter(Source.tns_name.isnot(None))

        all_matches = list(q)
        info['totalMatches'] = len(all_matches)
        info['sources'] = all_matches[
            ((page - 1) * SOURCES_PER_PAGE):(page * SOURCES_PER_PAGE)]
        info['lastPage'] = info['totalMatches'] <= page * SOURCES_PER_PAGE
        info['sourceNumberingStart'] = (page - 1) * SOURCES_PER_PAGE + 1
        info['sourceNumberingEnd'] = min(info['totalMatches'],
                                         page * SOURCES_PER_PAGE)
        if info['totalMatches'] == 0:
            info['sourceNumberingStart'] = 0

        return self.success(data=info)
