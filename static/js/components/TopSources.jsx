import React from 'react';
import { useSelector } from 'react-redux';
import { Link } from 'react-router-dom';

import * as profileActions from '../ducks/profile';
import WidgetPrefsDialog from './WidgetPrefsDialog';

import styles from "./TopSources.css";

const defaultPrefs = {
  maxNumSources: "",
  sinceDaysAgo: ""
};

const TopSources = () => {
  const { sourceViews } = useSelector((state) => state.topSources);
  const topSourcesPrefs = useSelector(
    (state) => state.profile.preferences.topSources
  ) || defaultPrefs;

  return (
    <div className={styles.topSourcesContainer}>
      <h2 style={{ display: "inline-block" }}>
        Top Sources
      </h2>
      <div style={{ display: "inline-block", float: "right" }}>
        <WidgetPrefsDialog
          formValues={topSourcesPrefs}
          stateBranchName="topSources"
          title="Top Sources Preferences"
          onSubmit={profileActions.updateUserPreferences}
        />
      </div>
      <p>
        Displaying most-viewed sources
      </p>
      <ul className={styles.topSourceList}>
        {
          sourceViews.map(({ obj_id, views, public_url }) => (
            <li key={`topSources_${obj_id}_${views}`} className={styles.topSource}>
              <Link to={`/source/${obj_id}`}>
                <img className={styles.stamp} src={public_url} alt={obj_id} />
              </Link>
              <div>
                &nbsp;
                -&nbsp;
                <Link to={`/source/${obj_id}`}>
                  {obj_id}
                </Link>
              </div>
              <div>
                <em>
                  &nbsp;
                  -&nbsp;
                  {views}
                  &nbsp;view(s)
                </em>
              </div>
            </li>
          ))
        }
      </ul>
    </div>
  );
};

export default TopSources;
