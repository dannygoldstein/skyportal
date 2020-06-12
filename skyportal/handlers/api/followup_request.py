import arrow
from marshmallow.exceptions import ValidationError

from baselayer.app.access import auth_or_token
from ..base import BaseHandler
from ...models import (DBSession, Instrument, Source, Token, ObservingRun,
                       RoboticFollowupRequest, Assignment)
from ...schema import AssignmentSchema, RoboticRequestSchema


class AssignmentHandler(BaseHandler):

    @auth_or_token
    def post(self):
        """
        ---
        description: Post new target assignment to observing run
        requestBody:
          content:
            application/json:
              schema:
                allOf:
                  - $ref: "#/components/schemas/AssignmentSchema"
                  - type: object
                    properties:
                      observations:
                        type: array
                        items:
                          anyOf:
                            - $ref: "#/components/schemas/ClassicalImaging"
                            - $ref: "#/components/schemas/ClassicalSpectroscopy"
                          discriminator:
                            propertyName: type
                            mapping:
                              imaging: "#/components/schemas/ClassicalImaging"
                              spectroscopy: "#/components/schemas/ClassicalSpectroscopy"
                        minItems: 1
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
                              description: New follow-up request ID
        """

        data = self.get_json()

        try:
            assignment = AssignmentSchema.load(data=data)
        except ValidationError as e:
            return self.error(f'Error parsing followup request: '
                              f'"{e.normalized_messages()}"')

        obj_id = assignment.obj_id
        source = Source.get_if_owned_by(obj_id, self.current_user)
        if source is None:
            return self.error(f'Invalid obj_id: "{obj_id}"')

        assignment.requester_id = self.associated_user_object.id
        DBSession.add(assignment)
        DBSession.commit()

        self.push_all(
            action="skyportal/REFRESH_SOURCE",
            payload={"obj_id": assignment.obj_id},
        )
        return self.success(data={"id": assignment.id})


    @auth_or_token
    def delete(self, assignment_id):
        """
        ---
        description: Delete assignment.
        parameters:
          - in: path
            name: assignment_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        assignment = Assignment.query.get(int(assignment_id))
        if hasattr(self.current_user, "roles"):
            if not (
                "Super admin" in [role.id for role in self.current_user.roles]
                or "Group admin" in [role.id for role in self.current_user.roles]
                or assignment.requester.username == self.current_user.username
            ):
                return self.error("Insufficient permissions.")
        elif isinstance(self.current_user, Token):
            if self.current_user.created_by_id != assignment.requester.id:
                return self.error("Insufficient permissions.")
        DBSession.delete(assignment)
        DBSession.commit()

        self.push_all(
            action="skyportal/REFRESH_SOURCE",
            payload={"obj_id": assignment.obj_id},
        )
        return self.success()



class FollowupRequestHandler(BaseHandler):


    @auth_or_token
    def post(self):
        """
        ---
        description: Submit a new robotic follow-up request
        requestBody:
          content:
            application/json:
              schema:
                allOf:
                  - $ref: "#/components/schemas/RoboticRequestSchema"
                  - type: object
                    properties:
                      observations:
                        type: array
                        items:
                          anyOf:
                            - $ref: "#/components/schemas/RoboticImaging"
                            - $ref: "#/components/schemas/RoboticSpectroscopy"
                          discriminator:
                            propertyName: type
                            mapping:
                              imaging: "#/components/schemas/RoboticImaging"
                              spectroscopy: "#/components/schemas/RoboticSpectroscopy"
                        minItems: 1
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
                              description: New follow-up request ID
        """

        data = self.get_json()

        # super basic validation - note we do not want the output
        # of this line, we want to keep the user-passed json
        try:
            request = RoboticRequestSchema.load(data=data)
        except ValidationError as e:
            return self.error(f'Error parsing followup request: '
                              f'"{e.normalized_messages()}"')

        obj_id = request.obj_id
        source = Source.get_if_owned_by(obj_id, self.current_user)
        if source is None:
            return self.error(f'Invalid obj_id: "{obj_id}"')

        request.requester_id = self.associated_user_object.id
        DBSession.add(request)
        DBSession.commit()

        self.push_all(
            action="skyportal/REFRESH_SOURCE",
            payload={"obj_id": request.obj_id},
        )
        return self.success(data={"id": request.id})



    @auth_or_token
    def delete(self, request_id):
        """
        ---
        description: Delete follow-up request.
        parameters:
          - in: path
            name: request_id
            required: true
            schema:
              type: string
        responses:
          200:
            content:
              application/json:
                schema: Success
        """
        followup_request = RoboticFollowupRequest.query.get(int(request_id))
        if hasattr(self.current_user, "roles"):
            if not (
                "Super admin" in [role.id for role in self.current_user.roles]
                or "Group admin" in [role.id for role in self.current_user.roles]
                or followup_request.requester.username == self.current_user.username
            ):
                return self.error("Insufficient permissions.")
        elif isinstance(self.current_user, Token):
            if self.current_user.created_by_id != followup_request.requester.id:
                return self.error("Insufficient permissions.")
        DBSession.delete(followup_request)
        DBSession.commit()

        self.push_all(
            action="skyportal/REFRESH_SOURCE",
            payload={"obj_id": followup_request.obj_id},
        )
        return self.success()
