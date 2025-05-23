"""add is_verified to developer table

Revision ID: e163cd45ddf7
Revises: a9d876df1fdf
Create Date: 2025-04-14 05:21:36.052375

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e163cd45ddf7'
down_revision: Union[str, None] = 'a9d876df1fdf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('developers', sa.Column('is_verified', sa.Boolean(), nullable=False))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('developers', 'is_verified')
    # ### end Alembic commands ###
