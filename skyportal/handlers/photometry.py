import os
import io
import base64
import tornado.web
from astropy.time import Time
from sqlalchemy.orm import joinedload
from marshmallow.exceptions import ValidationError
from PIL import Image
from baselayer.app.access import permissions, auth_or_token
from .base import BaseHandler
from ..models import DBSession, Photometry, Comment, Instrument, Source, Thumbnail


class PhotometryHandler(BaseHandler):
    @permissions(['Upload data'])
    def post(self):
        """
        ---
        description: Upload photometry
        parameters:
          - in: path
            name: photometry
            schema: Photometry
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - Success
                    - type: object
                      properties:
                        ids:
                          type: array
                          description: List of new photometry IDs
        """
        data = self.get_json()

        # TODO should filters be a table/plaintext/limited set of strings?
        if 'time_format' not in data or 'time_scale' not in data:
            return self.error('Time scale (\'time_scale\') and time format '
                              '(\'time_format\') are required parameters.')
        if not isinstance(data['mag'], (list, tuple)):
            data['time'] = [data['time']]
            data['mag'] = [data['mag']]
            data['e_mag'] = [data['e_mag']]
        ids = []
        instrument = Instrument.query.get(data['instrument_id'])
        if not instrument:
            raise Exception('Invalid instrument ID') # TODO: handle invalid instrument ID
        source = Source.query.get(data['source_id'])
        if not source:
            raise Exception('Invalid source ID') # TODO: handle invalid source ID
        for i in range(len(data['mag'])):
            if not (data['time_scale'] == 'tcb' and data['time_format'] == 'iso'):
                t = Time(data['time'][i],
                         format=data['time_format'],
                         scale=data['time_scale'])
                time = t.tcb.iso
            else:
                time = data['time'][i]
            p = Photometry(source=source,
                           observed_at=time,
                           mag=data['mag'][i],
                           e_mag=data['e_mag'][i],
                           time_scale='tcb',
                           time_format='iso',
                           instrument=instrument,
                           lim_mag=data['lim_mag'],
                           filter=data['filter'])
            DBSession().add(p)
            DBSession().flush()
            ids.append(p.id)
        if 'thumbnails' in data:
            for thumb in data['thumbnails']:
                file_uri = os.path.abspath(
                    f'static/thumbnails/{source.id}_{thumb["type"]}.png')
                file_bytes = base64.b64decode(thumb['data'])
                im = Image.open(io.BytesIO(file_bytes))
                if im.format != 'PNG':
                    return self.error('Invalid thumbnail image type. Only PNG are supported.')
                if im.size != (200, 200):
                    return self.error('Invalid thumbnail size. Only 200x200 px allowed.')
                with open(file_uri, 'wb') as f:
                    f.write(file_bytes)
                t = Thumbnail(type=thumb['type'],
                              photometry_id=ids[0],
                              file_uri=file_uri,
                              public_url=f'/static/thumbnails/{thumb["fname"]}')
                DBSession.add(t)
        DBSession().commit()

        return self.success(data={"ids": ids})

    @auth_or_token
    def get(self, photometry_id):
        info = {}
        info['photometry'] = Photometry.query.get(photometry_id)

        if info['photometry'] is not None:
            return self.success(data=info)
        else:
            return self.error(f"Could not load photometry {photometry_id}",
                              data={"photometry_id": photometry_id})

    @permissions(['Manage sources'])
    def put(self, photometry_id):
        data = self.get_json()
        data['id'] = photometry_id

        schema = Photometry.__schema__()
        try:
            schema.load(data)
        except ValidationError as e:
            return self.error('Invalid/missing parameters: '
                              f'{e.normalized_messages()}')
        DBSession().commit()

        return self.success()

    @permissions(['Manage sources'])
    def delete(self, photometry_id):
        DBSession.query(Photometry).filter(Photometry.id == int(photometry_id)).delete()
        DBSession().commit()

        return self.success()
