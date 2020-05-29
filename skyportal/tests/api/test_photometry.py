import os
import datetime
import base64
from skyportal.tests import api
from skyportal.models import Thumbnail, DBSession, Photometry

import numpy as np
from sncosmo.photdata import PhotometricData


def test_token_user_post_get_photometry_data(upload_data_token, public_source,
                                             ztf_camera):
    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'flux': 12.24,
                             'fluxerr': 0.031,
                             'zp': 25.,
                             'magsys': 'ab',
                             'filter': 'ztfg'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    np.testing.assert_allclose(data['data']['flux'],
                               12.24 * 10**(-0.4 * (25. - 23.9)))


def test_token_user_post_mag_photometry_data(upload_data_token, public_source,
                                             ztf_camera):

    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'mag': 21.,
                             'magerr': 0.2,
                             'limiting_mag': 22.3,
                             'magsys': 'ab',
                             'filter': 'ztfg'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    np.testing.assert_allclose(data['data']['flux'],
                               10**(-0.4 * (21. - 23.9)))

    np.testing.assert_allclose(data['data']['fluxerr'],
                               0.2 / (2.5 / np.log(10)) * data['data']['flux'])


def test_token_user_post_photometry_limits(upload_data_token, public_source,
                                           ztf_camera):

    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'mag': None,
                             'magerr': None,
                             'limiting_mag': 22.3,
                             'magsys': 'ab',
                             'filter': 'ztfg'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    assert data['data']['flux'] == None
    np.testing.assert_allclose(data['data']['fluxerr'],
                               10**(-0.4 * (22.3 - 23.9)) / 5)

    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'flux': None,
                             'fluxerr': 0.031,
                             'zp': 25.,
                             'magsys': 'ab',
                             'filter': 'ztfg'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    assert data['data']['flux'] == None
    np.testing.assert_allclose(data['data']['fluxerr'],
                               0.031 * 10**(-0.4 * (25. - 23.9)))


def test_token_user_post_invalid_filter(upload_data_token, public_source,
                                        ztf_camera):

    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'mag': None,
                             'magerr': None,
                             'limiting_mag': 22.3,
                             'magsys': 'ab',
                             'filter': 'bessellv'
                             },
                       token=upload_data_token)
    assert status == 400
    assert data['status'] == 'error'


def test_token_user_post_photometry_data_series(upload_data_token, public_source,
                                                ztf_camera):
    # valid request
    status, data = api(
        'POST',
        'photometry',
        data={'obj_id': str(public_source.id),
              'mjd': [58000., 58001., 58002.],
              'instrument_id': ztf_camera.id,
              'flux': [12.24, 15.24, 12.24],
              'fluxerr': [0.031, 0.029, 0.030],
              'filter': ['ztfg', 'ztfg', 'ztfg'],
              'zp': [25., 30., 21.2],
              'magsys': ['ab', 'ab', 'ab']},
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'
    assert len(data['data']['ids']) == 3

    photometry_id = data['data']['ids'][1]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'
    assert np.allclose(data['data']['flux'],
                       15.24 * 10**(-0.4 * (30 - 23.9)))

    # invalid request
    status, data = api(
        'POST',
        'photometry',
        data=[{'obj_id': str(public_source.id),
              'mjd': 58000,
              'instrument_id': ztf_camera.id,
              'flux': 12.24,
              'fluxerr': 0.031,
              'filter': 'ztfg',
              'zp': 25.,
              'magsys': 'ab'},
              {'obj_id': str(public_source.id),
               'mjd': 58001,
               'instrument_id': ztf_camera.id,
               'flux': 15.24,
               'fluxerr': 0.031,
               'filter': 'ztfg',
               'zp': 30.,
               'magsys': 'ab'},
              {'obj_id': str(public_source.id),
               'mjd': 58002,
               'instrument_id': ztf_camera.id,
               'flux': 12.24,
               'fluxerr': 0.031,
               'filter': 'ztfg',
               'zp': 21.2,
               'magsys': 'vega'}],
        token=upload_data_token)

    assert status == 400
    assert data['status'] == 'error'


def test_post_photometry_no_access_token(view_only_token, public_source,
                                         ztf_camera):
    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'flux': 12.24,
                             'fluxerr': 0.031,
                             'zp': 25.,
                             'magsys': 'ab',
                             'filter': 'ztfg'
                             },
                       token=view_only_token)
    assert status == 400
    assert data['status'] == 'error'


def test_token_user_update_photometry(upload_data_token,
                                      manage_sources_token,
                                      public_source, ztf_camera):
    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'flux': 12.24,
                             'fluxerr': 0.031,
                             'zp': 25.,
                             'magsys': 'ab',
                             'filter': 'ztfi'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'
    np.testing.assert_allclose(data['data']['flux'],
                               12.24 * 10**(-0.4 * (25 - 23.9)))

    status, data = api(
        'PUT',
        f'photometry/{photometry_id}',
        data={'obj_id': str(public_source.id),
              'flux': 11.0,
              'mjd': 58000.,
              'instrument_id': ztf_camera.id,
              'fluxerr': 0.031,
              'zp': 25.,
              'magsys': 'ab',
              'filter': 'ztfi'},
        token=manage_sources_token)
    assert status == 200
    assert data['status'] == 'success'

    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    np.testing.assert_allclose(data['data']['flux'],
                               11.0 * 10**(-0.4 * (25 - 23.9)))


def test_delete_photometry_data(upload_data_token, manage_sources_token,
                                public_source, ztf_camera):
    status, data = api('POST', 'photometry',
                       data={'obj_id': str(public_source.id),
                             'mjd': 58000.,
                             'instrument_id': ztf_camera.id,
                             'flux': 12.24,
                             'fluxerr': 0.031,
                             'zp': 25.,
                             'magsys': 'ab',
                             'filter': 'ztfi'
                             },
                       token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'

    photometry_id = data['data']['ids'][0]
    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 200
    assert data['status'] == 'success'
    np.testing.assert_allclose(data['data']['flux'],
                               12.24 * 10 ** (-0.4 * (25 - 23.9)))

    status, data = api(
        'DELETE',
        f'photometry/{photometry_id}',
        token=manage_sources_token)
    assert status == 200

    status, data = api(
        'GET',
        f'photometry/{photometry_id}?format=flux',
        token=upload_data_token)
    assert status == 400

