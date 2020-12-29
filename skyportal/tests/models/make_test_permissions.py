import os

access_types = ['create', 'read', 'update', 'delete']
users = ['user', 'user_group2', 'super_admin_user', 'group_admin_user']
targets = [
    'public_group',
    'public_groupuser',
    'public_stream',
    'public_groupstream',
    'public_streamuser',
    'public_filter',
    'public_candidate_object',
    'public_source_object',
    'keck1_telescope',
    'sedm',
    'public_group_sedm_allocation',
    'public_group_taxonomy',
    'taxonomy',
]


directory = os.path.dirname(__file__)
fname = os.path.join(directory, 'test_permissions.py')

with open(fname, 'w') as f:
    for user in users:
        for access_type in access_types:
            for target in targets:
                test = f"""
def test_{user}_{access_type}_{target}({user}, {target}, benchmark):
    def check_accessibility():
        accessible = {target}.is_accessible_by({user}, mode="{access_type}")
        assert accessible == accessible
    benchmark(check_accessibility)

"""
                f.write(test)
