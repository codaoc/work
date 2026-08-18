"""Add db_type column to databaseconnections table.

Revision ID: 002
Revises: 001
Create Date: 2026-08-17

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add db_type column to databaseconnections table."""
    # Add db_type column with default value
    with op.batch_alter_table('databaseconnections') as batch_op:
        batch_op.add_column(sa.Column('db_type', sa.String(length=20), nullable=True, server_default='postgresql'))


def downgrade() -> None:
    """Remove db_type column from databaseconnections table."""
    with op.batch_alter_table('databaseconnections') as batch_op:
        batch_op.drop_column('db_type')