"""scanning page filtering updates

Revision ID: 1300e24dcfe9
Revises: e1141138d4c6
Create Date: 2020-10-29 18:45:35.520499

"""
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '1300e24dcfe9'
down_revision = 'e1141138d4c6'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'candidates', 'passed_at', existing_type=postgresql.TIMESTAMP(), nullable=False
    )
    op.create_index(
        op.f('ix_candidates_passed_at'), 'candidates', ['passed_at'], unique=False
    )


def downgrade():
    op.drop_index(op.f('ix_candidates_passed_at'), table_name='candidates')
    op.alter_column(
        'candidates', 'passed_at', existing_type=postgresql.TIMESTAMP(), nullable=True
    )
