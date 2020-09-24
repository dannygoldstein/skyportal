from marshmallow.exceptions import ValidationError
from baselayer.app.access import permissions, auth_or_token
from ..base import BaseHandler
from ...models import DBSession, Group, Instrument, Obj, Source, Spectrum
from ...schema import SpectrumAsciiFilePostJSON


class SpectrumHandler(BaseHandler):
    @permissions(['Upload data'])
    def post(self):
        """
        ---
        description: Upload spectrum
        requestBody:
          content:
            application/json:
              schema:
                allOf:
                  - $ref: '#/components/schemas/SpectrumNoID'
                  - type: object
                    properties:
                      group_ids:
                        type: array
                        items:
                          type: integer
                        description: Group IDs that spectrum will be associated with
                    required:
                      - group_ids
        responses:
          200:
            content:
              application/json:
                schema:
                  allOf:
                    - $ref: '#/components/schemas/Success'
                    - type: object
                      properties:
                        data:
                          type: object
                          properties:
                            id:
                              type: integer
                              description: New spectrum ID
          400:
            content:
              application/json:
                schema: Error
        """
        data = self.get_json()
        instrument_id = data.get('instrument_id')
        if isinstance(instrument_id, list):
            if not all(instrument == instrument_id[0] for instrument in instrument_id):
                return self.error('Can only upload data for one instrument at a time')
            else:
                instrument_id = instrument_id[0]
        try:
            group_ids = data.pop("group_ids")
        except KeyError:
            return self.error("Missing required field: group_ids")
        groups = Group.query.filter(Group.id.in_(group_ids)).all()

        instrument = Instrument.query.get(instrument_id)

        schema = Spectrum.__schema__()
        try:
            spec = schema.load(data)
        except ValidationError as e:
            return self.error(
                'Invalid/missing parameters: ' f'{e.normalized_messages()}'
            )
        spec.instrument = instrument
        spec.groups = groups
        DBSession().add(spec)
        DBSession().commit()

        return self.success(data={"id": spec.id})

    @auth_or_token
    def get(self, spectrum_id):
        """
        ---
        description: Retrieve a spectrum
        parameters:
          - in: path
            name: spectrum_id
            required: true
            schema:
              type: integer
        responses:
          200:
            content:
              application/json:
                schema: SingleSpectrum
          400:
            content:
              application/json:
                schema: Error
        """
        spectrum = Spectrum.query.get(spectrum_id)

        if spectrum is not None:
            # Permissions check
            _ = Source.get_obj_if_owned_by(spectrum.obj_id, self.current_user)
            return self.success(data=spectrum)
        else:
            return self.error(f"Could not load spectrum with ID {spectrum_id}")

    @permissions(['Manage sources'])
    def put(self, spectrum_id):
        """
        ---
        description: Update spectrum
        parameters:
          - in: path
            name: spectrum_id
            required: true
            schema:
              type: integer
        requestBody:
          content:
            application/json:
              schema: SpectrumNoID
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
        spectrum = Spectrum.query.get(spectrum_id)
        # Permissions check
        _ = Source.get_obj_if_owned_by(spectrum.obj_id, self.current_user)
        data = self.get_json()
        data['id'] = spectrum_id

        schema = Spectrum.__schema__()
        try:
            schema.load(data, partial=True)
        except ValidationError as e:
            return self.error(
                'Invalid/missing parameters: ' f'{e.normalized_messages()}'
            )
        DBSession().commit()

        return self.success()

    @permissions(['Manage sources'])
    def delete(self, spectrum_id):
        """
        ---
        description: Delete a spectrum
        parameters:
          - in: path
            name: spectrum_id
            required: true
            schema:
              type: integer
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
        spectrum = Spectrum.query.get(spectrum_id)
        # Permissions check
        _ = Source.get_obj_if_owned_by(spectrum.obj_id, self.current_user)
        DBSession().delete(spectrum)
        DBSession().commit()

        return self.success()


class SpectrumASCIIFileHandler(BaseHandler):
    @permissions(['Upload data'])
    def post(self):
        """
        ---
        description: Upload spectrum from ASCII file
        requestBody:
          content:
            application/json:
              schema: SpectrumAsciiFilePostJSON
            application/octet-stream:
              schema:
                type: string
                format: binary
                title: Spectrum ASCII File
                required: true
        responses:
          200:
            content:
              application/json:
                schema: SpectrumNoID
          400:
            content:
              application/json:
                schema: Error
        """

        json = self.get_json()

        # validate the JSON
        try:
            json = SpectrumAsciiFilePostJSON.load(json)
        except ValidationError as e:
            return self.error(
                'Invalid/missing parameters: ' f'{e.normalized_messages()}'
            )

        obj = Source.get_obj_if_owned_by(json['obj_id'], self.current_user)
        if obj is None:
            return self.error('Invalid Obj id.')

        instrument = Instrument.query.get(json['instrument_id'])
        if instrument is None:
            return self.error('Invalid instrument id.')

        group_ids = json.pop('group_ids')
        user_group_ids = [g.id for g in self.current_user.accessible_groups]
        for group_id in group_ids:
            if group_id not in user_group_ids:
                return self.error('Insufficient permissions.')

        filename = json.pop('filename')
        ascii = json.pop('ascii')
        spec = Spectrum.from_ascii(filename, data=ascii, **json)
        return self.success(data=spec)


class ObjSpectraHandler(BaseHandler):
    @auth_or_token
    def get(self, obj_id):
        """
        ---
        description: Retrieve all spectra associated with an Object
        parameters:
          - in: path
            name: obj_id
            required: true
            schema:
              type: string
            description: ID of the object to retrieve spectra for
        responses:
          200:
            content:
              application/json:
                schema: ArrayOfSpectrums
          400:
            content:
              application/json:
                schema: Error
        """

        obj = Obj.query.get(obj_id)
        if obj is None:
            return self.error('Invalid object ID.')
        spectra = Obj.get_spectra_owned_by(obj_id, self.current_user)
        return_values = []
        for spec in spectra:
            spec_dict = spec.to_dict()
            spec_dict["instrument_name"] = spec.instrument.name
            spec_dict["groups"] = spec.groups
            return_values.append(spec_dict)
        return self.success(data=return_values)
