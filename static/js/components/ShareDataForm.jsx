import React, { useEffect, useState, Suspense } from "react";
import PropTypes from "prop-types";
import { Link } from "react-router-dom";
import { useSelector, useDispatch } from "react-redux";
import MUIDataTable from "mui-datatables";
import { makeStyles } from "@material-ui/core/styles";
import Typography from "@material-ui/core/Typography";
import { useForm, Controller } from "react-hook-form";
import Autocomplete from "@material-ui/lab/Autocomplete";
import Button from "@material-ui/core/Button";
import TextField from "@material-ui/core/TextField";
import Paper from "@material-ui/core/Paper";
import IconButton from "@material-ui/core";
import DeleteForeverIcon from "@material-ui/icons/DeleteForever";
import Dialog from "@material-ui/core/Dialog";
import DialogContent from "@material-ui/core/DialogContent";

import { showNotification } from "baselayer/components/Notifications";

import FormValidationError from "./FormValidationError";

import * as photometryActions from "../ducks/photometry";
import * as spectraActions from "../ducks/spectra";
import * as sourceActions from "../ducks/source";
import { useSourceStyles } from "./SourceDesktop";
import { deleteSpectrum } from "../ducks/spectra";

const UserContactLink = ({ user }) => {
  const display_string =
    user.first_name && user.last_name
      ? `${user.first_name} ${user.last_name}`
      : user.username;
  return (
    <div>
      {user.contact_email && (
        <a href={`mailto:${user.contact_email}`}>{display_string}</a>
      )}
      {!user.contact_email && <p>{display_string}</p>}
    </div>
  );
};

UserContactLink.propTypes = {
  user: PropTypes.shape({
    first_name: PropTypes.string,
    last_name: PropTypes.string,
    username: PropTypes.string.isRequired,
    contact_email: PropTypes.string,
  }).isRequired,
};

const Plot = React.lazy(() => import(/* webpackChunkName: "Bokeh" */ "./Plot"));

const createPhotRow = (
  id,
  mjd,
  mag,
  magerr,
  limiting_mag,
  instrument,
  filter,
  groups
) => ({
  id,
  mjd: Number(mjd).toFixed(3),
  mag: mag === null ? null : Number(mag).toFixed(4),
  magerr: magerr === null ? null : Number(magerr).toFixed(4),
  limiting_mag: Number(limiting_mag).toFixed(4),
  instrument,
  filter,
  groups,
});

const createSpecRow = (
  id,
  instrument,
  observed,
  groups,
  uploader,
  reducers,
  observers
) => ({
  id,
  instrument,
  observed,
  groups,
  uploader,
  reducers,
  observers,
});

const photHeadCells = [
  { name: "id", label: "ID" },
  { name: "mjd", label: "MJD" },
  { name: "mag", label: "Mag" },
  { name: "magerr", label: "Mag Error" },
  { name: "limiting_mag", label: "Limiting Mag" },
  { name: "instrument", label: "Instrument" },
  { name: "filter", label: "Filter" },
  { name: "groups", label: "Currently visible to" },
];

const useStyles = makeStyles(() => ({
  groupSelect: {
    width: "20rem",
  },
}));

const SpectrumRow = ({ rowData, route }) => {
  const styles = useSourceStyles();
  return (
    <div>
      <Paper className={styles.photometryContainer}>
        <Suspense fallback={<div>Loading spectroscopy plot...</div>}>
          <Plot
            className={styles.plot}
            // eslint-disable-next-line react/prop-types
            url={`/api/internal/plot/spectroscopy/${route.id}?spectrumID=${rowData[0]}`}
          />
        </Suspense>
      </Paper>
    </div>
  );
};

SpectrumRow.propTypes = {
  route: PropTypes.string.isRequired,
  rowData: PropTypes.arrayOf(PropTypes.number).isRequired,
};

const ShareDataForm = ({ route }) => {
  const classes = useStyles();

  const dispatch = useDispatch();
  const [selectedPhotRows, setSelectedPhotRows] = useState([]);
  const [selectedSpecRows, setSelectedSpecRows] = useState([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { all: groups } = useSelector((state) => state.groups);
  const photometry = useSelector((state) => state.photometry);
  const spectra = useSelector((state) => state.spectra);

  const { handleSubmit, errors, reset, control, getValues } = useForm();

  useEffect(() => {
    dispatch(photometryActions.fetchSourcePhotometry(route.id));
    dispatch(spectraActions.fetchSourceSpectra(route.id));
  }, [route.id, dispatch]);

  const validateGroups = () => {
    const formState = getValues({ nest: true });
    return formState.groups.length >= 1;
  };

  const onSubmit = async (groupsFormData) => {
    const selectedPhotIDs = selectedPhotRows.map(
      (idx) => photometry[route.id][idx].id
    );
    const selectedSpecIDs = selectedSpecRows.map(
      (idx) => spectra[route.id][idx].id
    );
    setIsSubmitting(true);
    const data = {
      groupIDs: groupsFormData.groups.map((g) => g.id),
      photometryIDs: selectedPhotIDs,
      spectrumIDs: selectedSpecIDs,
    };
    const result = await dispatch(sourceActions.shareData(data));
    if (result.status === "success") {
      dispatch(showNotification("Data successfully shared"));
      reset({ groups: [] });
      setSelectedPhotRows([]);
      setSelectedSpecRows([]);
    }
    setIsSubmitting(false);
  };

  if ((!photometry[route.id] && !spectra[route.id]) || !groups) {
    return <>Loading...</>;
  }

  const photRows = photometry[route.id]
    ? photometry[route.id].map((phot) =>
        createPhotRow(
          phot.id,
          phot.mjd,
          phot.mag,
          phot.magerr,
          phot.limiting_mag,
          phot.instrument_name,
          phot.filter,
          phot.groups.map((group) => group.name).join(", ")
        )
      )
    : [];

  const specRows = spectra[route.id]
    ? spectra[route.id].map((spec) =>
        createSpecRow(
          spec.id,
          spec.instrument_name,
          spec.observed_at,
          spec.groups.map((group) => group.name).join(", "),
          spec.uploader,
          spec.reducers,
          spec.observers
        )
      )
    : [];

  const RenderSingleUser = (dataIndex) => {
    const user = specRows[dataIndex].uploader;
    if (user) {
      return <UserContactLink user={user} />;
    }
    return <div />;
  };

  const makeRenderMultipleUsers = (key) => {
    const RenderMultipleUsers = (dataIndex) => {
      const users = specRows[dataIndex][key];
      if (users) {
        return users.map((user) => (
          <UserContactLink user={user} key={user.id} />
        ));
      }
      return <div />;
    };
    return RenderMultipleUsers;
  };

  const DeleteSpectrumButton = (dataIndex) => {
    const specid = specRows[dataIndex].id;
    const [open, setOpen] = useState(false);
    return (
      <div>
        <Dialog
          open={open}
          aria-labelledby="simple-modal-title"
          aria-describedby="simple-modal-description"
          onClose={() => {
            setOpen(false);
          }}
          className={classes.detailedSpecButton}
        >
          <DialogContent>
            <div>
              <Typography variant="h6">
                Are you sure you want to do this?
              </Typography>
              The following operation <em>permanently</em> deletes the spectrum
              from the database. This operation cannot be undone and your data
              cannot be recovered after the fact. You will have to upload the
              spectrum again from scratch.
            </div>
            <div>
              <Button
                onClick={() => {
                  setOpen(false);
                }}
              >
                No, do not delete the spectrum.
              </Button>
              <Button
                onClick={async () => {
                  const result = await dispatch(deleteSpectrum(specid));
                  if (result.status === "success") {
                    dispatch(showNotification("Spectrum deleted."));
                  }
                }}
                data-testid="yes-delete"
              >
                Yes, delete the spectrum.
              </Button>
            </div>
          </DialogContent>
        </Dialog>
        <IconButton
          onClick={() => {
            setOpen(true);
          }}
          data-testid={`delete-spectrum-button-${specid}`}
        >
          <DeleteForeverIcon />
        </IconButton>
      </div>
    );
  };

  const specHeadCells = [
    { name: "id", label: "ID" },
    { name: "instrument", label: "Instrument" },
    { name: "observed", label: "Observed (UTC)" },
    { name: "groups", label: "Currently visible to" },
    {
      name: "uploader",
      label: "Uploaded by",
      options: { customBodyRenderLite: RenderSingleUser },
    },
    {
      name: "reducers",
      label: "Reduced by",
      options: {
        customBodyRenderLite: makeRenderMultipleUsers("reducers"),
      },
    },
    {
      name: "observers",
      label: "Observed by",
      options: { customBodyRenderLite: makeRenderMultipleUsers("observers") },
    },
    {
      name: "delete",
      label: "Delete Spectrum",
      options: { customBodyRenderLite: DeleteSpectrumButton },
    },
  ];

  const options = {
    textLabels: {
      body: {
        noMatch: "",
      },
    },
    filter: true,
    selectableRows: "multiple",
    filterType: "dropdown",
    responsive: "vertical",
    rowsPerPage: 10,
    selectableRowsHeader: true,
    customToolbarSelect: () => {},
    download: false,
    print: false,
  };

  return (
    <>
      <div>
        <Typography variant="h5">
          Share Source Data -&nbsp;
          <Link to={`/source/${route.id}`} role="link">
            {route.id}
          </Link>
        </Typography>
        <p>
          This page allows you to share data for {`${route.id}`} with other
          users or groups. Select the photometry or spectra you would like to
          share from the list below, then select the users or groups you would
          like to share the data with. When you click submit, the access
          permissions on the data will be updated. Data shared via this page
          will not cause the source to be saved to another group.
        </p>
      </div>
      <br />
      <div>
        {!!photometry[route.id] && (
          <MUIDataTable
            columns={photHeadCells}
            data={photRows}
            title="Photometry"
            options={{
              ...options,
              rowsSelected: selectedPhotRows,
              onRowSelectionChange: (
                rowsSelectedData,
                allRows,
                rowsSelected
              ) => {
                setSelectedPhotRows(rowsSelected);
              },
              selectableRowsOnClick: true,
            }}
          />
        )}
        <br />
        {!!spectra[route.id] && (
          <MUIDataTable
            columns={specHeadCells}
            data={specRows}
            title="Spectra"
            options={{
              ...options,
              rowsSelected: selectedSpecRows,
              onRowSelectionChange: (
                rowsSelectedData,
                allRows,
                rowsSelected
              ) => {
                setSelectedSpecRows(rowsSelected);
              },
              expandableRows: true,
              // eslint-disable-next-line react/display-name,no-unused-vars
              renderExpandableRow: (rowData, rowMeta) => (
                <SpectrumRow rowData={rowData} route={route} />
              ),
              expandableRowsOnClick: true,
            }}
          />
        )}
      </div>
      <br />
      <div>
        <form onSubmit={handleSubmit(onSubmit)}>
          {!!errors.groups && (
            <FormValidationError message="Please select at least one group/user" />
          )}
          <Controller
            name="groups"
            render={({ onChange, value, ...props }) => (
              <Autocomplete
                multiple
                id="dataSharingFormGroupsSelect"
                options={groups}
                value={value}
                onChange={(e, data) => onChange(data)}
                getOptionLabel={(group) => group.name}
                filterSelectedOptions
                renderInput={(params) => (
                  <TextField
                    // eslint-disable-next-line react/jsx-props-no-spreading
                    {...params}
                    error={!!errors.groups}
                    variant="outlined"
                    label="Select Groups/Users"
                    className={classes.groupSelect}
                  />
                )}
                // eslint-disable-next-line react/jsx-props-no-spreading
                {...props}
              />
            )}
            control={control}
            rules={{ validate: validateGroups }}
            defaultValue={[]}
          />
          <br />
          <div>
            <Button
              variant="contained"
              type="submit"
              name="submitShareButton"
              disabled={isSubmitting}
            >
              Submit
            </Button>
          </div>
          <div style={{ display: isSubmitting ? "block" : "none" }}>
            Processing...
          </div>
        </form>
      </div>
    </>
  );
};
ShareDataForm.propTypes = {
  route: PropTypes.shape({
    id: PropTypes.string,
  }).isRequired,
};

export default ShareDataForm;
