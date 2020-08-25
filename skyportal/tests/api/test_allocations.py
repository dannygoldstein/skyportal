from skyportal.tests import api


def test_super_user_post_allocation(sedm, public_group, super_admin_token):

    request_data = {
        'group_id': public_group.id,
        'instrument_id': sedm.id,
        'pi': 'Shri Kulkarni',
        'hours_allocated': 200,
        'start_date': '3021-02-27T00:00:00',
        'end_date': '3021-07-20T00:00:00',
        'proposal_id': 'COO-2020A-P01',
    }

    status, data = api('POST', 'allocation', data=request_data, token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'
    id = data['data']['id']

    status, data = api('GET', f'allocation/{id}', token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'

    for key in request_data:
        assert data['data'][key] == request_data[key]


def test_super_user_modify_allocation(sedm, public_group, super_admin_token):

    request_data = {
        'group_id': public_group.id,
        'instrument_id': sedm.id,
        'pi': 'Shri Kulkarni',
        'hours_allocated': 200,
        'start_date': '3021-02-27T00:00:00',
        'end_date': '3021-07-20T00:00:00',
        'proposal_id': 'COO-2020A-P01',
    }

    status, data = api('POST', 'allocation', data=request_data, token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'
    id = data['data']['id']

    status, data = api('GET', f'allocation/{id}', token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'

    for key in request_data:
        assert data['data'][key] == request_data[key]

    request2_data = {'proposal_id': 'COO-2020A-P02'}

    status, data = api(
        'PUT', f'allocation/{id}', data=request2_data, token=super_admin_token
    )
    assert status == 200

    status, data = api('GET', f'allocation/{id}', token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'

    request_data.update(request2_data)
    for key in request_data:
        assert data['data'][key] == request_data[key]


def test_read_only_user_cannot_get_unowned_allocation(
    view_only_token, super_admin_token, sedm, public_group2
):

    request_data = {
        'group_id': public_group2.id,
        'instrument_id': sedm.id,
        'pi': 'Shri Kulkarni',
        'hours_allocated': 200,
        'start_date': '3021-02-27T00:00:00',
        'end_date': '3021-07-20T00:00:00',
        'proposal_id': 'COO-2020A-P01',
    }

    status, data = api('POST', 'allocation', data=request_data, token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'
    id = data['data']['id']

    status, data = api('GET', f'allocation/{id}', token=super_admin_token)
    assert status == 200
    assert data['status'] == 'success'

    for key in request_data:
        assert data['data'][key] == request_data[key]

    status, data = api('GET', f'allocation/{id}', token=view_only_token)
    assert status == 400
    assert data['status'] == 'error'
