import { combineReducers } from 'redux';

import { reducer as notificationsReducer } from 'baselayer/components/Notifications';

import * as Action from './actions';

// Reducer for currently displayed source
export function sourceReducer(state={ source: null, loadError: false }, action) {
  switch (action.type) {
    case Action.FETCH_LOADED_SOURCE_OK: {
      const source = action.data;
      return {
        ...state,
        ...source,
        loadError: false
      };
    }
    case Action.FETCH_LOADED_SOURCE_FAIL:
      return {
        ...state,
        loadError: true
      };
    default:
      return state;
  }
}

export function sourcesReducer(state={ latest: null }, action) {
  switch (action.type) {
    case Action.FETCH_SOURCES_OK: {
      const sources = action.data;
      return {
        ...state,
        latest: sources
      };
    }
    default:
      return state;
  }
}

export function groupReducer(state={}, action) {
  switch (action.type) {
    case Action.FETCH_GROUP_OK:
      return action.data;
    default:
      return state;
  }
}

export function groupsReducer(state={ latest: [], all: null }, action) {
  switch (action.type) {
    case Action.FETCH_GROUPS_OK: {
      const { user_groups, all_groups } = action.data;
      return {
        ...state,
        latest: user_groups,
        all: all_groups
      };
    }
    default:
      return state;
  }
}

export function usersReducer(state={}, action) {
  switch (action.type) {
    case Action.FETCH_USER_OK: {
      const { id, ...user_info } = action.data;
      return {
        ...state,
        [id]: user_info
      };
    }
    default:
      return state;
  }
}


export function profileReducer(state={ username: '', roles: [], acls: [], tokens: [] }, action) {
  switch (action.type) {
    case Action.FETCH_USER_PROFILE_OK:
      return action.data;
    default:
      return state;
  }
}

export function plotsReducer(state={ plotData: {}, plotIDList: [] }, action) {
  switch (action.type) {
    case Action.FETCH_SOURCE_PLOT_OK: {
      const plotData = { ...state.plotData };
      const plotIDList = state.plotIDList.slice();

      const { url, ...incomingData } = action.data;
      plotIDList.unshift(url);
      plotData[url] = incomingData;
      if (plotIDList.length >= 40) {
        plotIDList.length = 40;
        Object.keys(plotData).forEach((ID) => {
          if (!plotIDList.includes(ID)) {
            delete plotData[ID];
          }
        });
      }
      return {
        plotData,
        plotIDList
      };
    }
    default:
      return state;
  }
}

export function miscReducer(state={ rotateLogo: false }, action) {
  switch (action.type) {
    case Action.ROTATE_LOGO: {
      return {
        ...state,
        rotateLogo: true
      };
    }
    default:
      return state;
  }
}

const root = combineReducers({
  source: sourceReducer,
  sources: sourcesReducer,
  group: groupReducer,
  groups: groupsReducer,
  notifications: notificationsReducer,
  profile: profileReducer,
  plots: plotsReducer,
  misc: miscReducer,
  users: usersReducer
});

export default root;
