import * as API from '../API';

export const REFRESH_GROUP = 'skyportal/REFRESH_GROUP';

export const FETCH_GROUP = 'skyportal/FETCH_GROUP';
export const FETCH_GROUP_OK = 'skyportal/FETCH_GROUP_OK';
const FETCH_GROUP_FAIL = 'skyportal/FETCH_GROUP_FAIL';

export function fetchGroup(id) {
  return API.GET(`/api/groups/${id}`, FETCH_GROUP);
}

export default function reducer(state={}, action) {
  switch (action.type) {
    case FETCH_GROUP_OK: {
      const { group } = action.data;
      return group;
    }
    case FETCH_GROUP_FAIL: {
      return {};
    }
    default:
      return state;
  }
}
