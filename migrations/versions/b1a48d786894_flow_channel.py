"""flow channel

Revision ID: b1a48d786894
Revises: c8f4a2d6e0b7
Create Date: 2026-11-25 10:41:27.800223

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b1a48d786894'
down_revision: str | Sequence[str] | None = 'c8f4a2d6e0b7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_channel_type"), "channel", type_="check")
    op.create_check_constraint(
        op.f("ck_channel_type"),
        "channel",
        "type IN ('talk', 'vote', 'doc', 'approval', 'work', 'flow')",
    )
    op.create_table('flow_approval_target',
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('target_channel_id', sa.Integer(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_flow_approval_target_channel_id_channel')),
    sa.ForeignKeyConstraint(['target_channel_id'], ['channel.id'], name=op.f('fk_flow_approval_target_target_channel_id_channel')),
    sa.PrimaryKeyConstraint('channel_id', 'target_channel_id', name=op.f('pk_flow_approval_target'))
    )
    op.create_table('flow_assignee',
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('step_key', sa.String(length=10), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_flow_assignee_channel_id_channel')),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_flow_assignee_user_id_user')),
    sa.PrimaryKeyConstraint('channel_id', 'step_key', 'user_id', name=op.f('pk_flow_assignee'))
    )
    op.create_index(op.f('ix_flow_assignee_user_id'), 'flow_assignee', ['user_id'], unique=False)
    op.create_table('flow_definition',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('published_by', sa.Integer(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_flow_definition_channel_id_channel')),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], name=op.f('fk_flow_definition_created_by_user')),
    sa.ForeignKeyConstraint(['published_by'], ['user.id'], name=op.f('fk_flow_definition_published_by_user')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_flow_definition')),
    sa.UniqueConstraint('channel_id', 'version', name='uq_flow_definition_version')
    )
    op.create_index('ix_flow_definition_draft', 'flow_definition', ['channel_id'], unique=True, postgresql_where=sa.text('version IS NULL'))
    op.create_table('flow_item',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('definition_id', sa.Integer(), nullable=False),
    sa.Column('registrant_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('current_step', sa.Integer(), nullable=False),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('closed_step', sa.Integer(), nullable=True),
    sa.Column('source_post_id', sa.Integer(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('draft', 'in_progress', 'returned', 'done', 'rejected', 'withdrawn', 'discarded')", name=op.f('ck_flow_item_status')),
    sa.ForeignKeyConstraint(['definition_id'], ['flow_definition.id'], name=op.f('fk_flow_item_definition_id_flow_definition')),
    sa.ForeignKeyConstraint(['post_id'], ['post.id'], name=op.f('fk_flow_item_post_id_post')),
    sa.ForeignKeyConstraint(['registrant_id'], ['user.id'], name=op.f('fk_flow_item_registrant_id_user')),
    sa.ForeignKeyConstraint(['source_post_id'], ['post.id'], name=op.f('fk_flow_item_source_post_id_post')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_flow_item')),
    sa.UniqueConstraint('post_id', name=op.f('uq_flow_item_post_id'))
    )
    op.create_index(op.f('ix_flow_item_definition_id'), 'flow_item', ['definition_id'], unique=False)
    op.create_index(op.f('ix_flow_item_registrant_id'), 'flow_item', ['registrant_id'], unique=False)
    op.create_index(op.f('ix_flow_item_status'), 'flow_item', ['status'], unique=False)
    op.create_table('flow_approval',
    sa.Column('approval_id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approval.id'], name=op.f('fk_flow_approval_approval_id_approval')),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], name=op.f('fk_flow_approval_created_by_user')),
    sa.ForeignKeyConstraint(['item_id'], ['flow_item.id'], name=op.f('fk_flow_approval_item_id_flow_item')),
    sa.PrimaryKeyConstraint('approval_id', name=op.f('pk_flow_approval'))
    )
    op.create_index(op.f('ix_flow_approval_item_id'), 'flow_approval', ['item_id'], unique=False)
    op.create_table('flow_pick',
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('step_key', sa.String(length=10), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['flow_item.id'], name=op.f('fk_flow_pick_item_id_flow_item')),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_flow_pick_user_id_user')),
    sa.PrimaryKeyConstraint('item_id', 'step_key', 'user_id', name=op.f('pk_flow_pick'))
    )
    op.create_index(op.f('ix_flow_pick_user_id'), 'flow_pick', ['user_id'], unique=False)
    op.create_table('flow_reply',
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('step_no', sa.Integer(), nullable=False),
    sa.Column('item_status', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['flow_item.id'], name=op.f('fk_flow_reply_item_id_flow_item')),
    sa.ForeignKeyConstraint(['post_id'], ['post.id'], name=op.f('fk_flow_reply_post_id_post')),
    sa.PrimaryKeyConstraint('post_id', name=op.f('pk_flow_reply'))
    )
    op.create_index(op.f('ix_flow_reply_item_id'), 'flow_reply', ['item_id'], unique=False)
    op.create_table('flow_submission',
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('picks', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['flow_item.id'], name=op.f('fk_flow_submission_item_id_flow_item')),
    sa.PrimaryKeyConstraint('item_id', 'round', name=op.f('pk_flow_submission'))
    )
    op.create_table('flow_task',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('step_no', sa.Integer(), nullable=False),
    sa.Column('assignee_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('decision', sa.String(length=10), nullable=True),
    sa.Column('return_to', sa.Integer(), nullable=True),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('acted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("decision IN ('finish', 'continue')", name=op.f('ck_flow_task_decision')),
    sa.CheckConstraint("status IN ('waiting', 'done', 'returned', 'rejected', 'skipped', 'replaced', 'voided')", name=op.f('ck_flow_task_status')),
    sa.ForeignKeyConstraint(['assignee_id'], ['user.id'], name=op.f('fk_flow_task_assignee_id_user')),
    sa.ForeignKeyConstraint(['item_id'], ['flow_item.id'], name=op.f('fk_flow_task_item_id_flow_item')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_flow_task'))
    )
    op.create_index('ix_flow_task_active', 'flow_task', ['item_id', 'round', 'step_no', 'assignee_id'], unique=True, postgresql_where=sa.text("status NOT IN ('replaced', 'voided')"))
    op.create_index(op.f('ix_flow_task_assignee_id'), 'flow_task', ['assignee_id'], unique=False)
    op.create_index('ix_flow_task_waiting', 'flow_task', ['assignee_id'], unique=False, postgresql_where=sa.text("status = 'waiting'"))


def downgrade() -> None:
    op.drop_index('ix_flow_task_waiting', table_name='flow_task', postgresql_where=sa.text("status = 'waiting'"))
    op.drop_index(op.f('ix_flow_task_assignee_id'), table_name='flow_task')
    op.drop_index('ix_flow_task_active', table_name='flow_task', postgresql_where=sa.text("status NOT IN ('replaced', 'voided')"))
    op.drop_table('flow_task')
    op.drop_table('flow_submission')
    op.drop_index(op.f('ix_flow_reply_item_id'), table_name='flow_reply')
    op.drop_table('flow_reply')
    op.drop_index(op.f('ix_flow_pick_user_id'), table_name='flow_pick')
    op.drop_table('flow_pick')
    op.drop_index(op.f('ix_flow_approval_item_id'), table_name='flow_approval')
    op.drop_table('flow_approval')
    op.drop_index(op.f('ix_flow_item_status'), table_name='flow_item')
    op.drop_index(op.f('ix_flow_item_registrant_id'), table_name='flow_item')
    op.drop_index(op.f('ix_flow_item_definition_id'), table_name='flow_item')
    op.drop_table('flow_item')
    op.drop_index('ix_flow_definition_draft', table_name='flow_definition', postgresql_where=sa.text('version IS NULL'))
    op.drop_table('flow_definition')
    op.drop_index(op.f('ix_flow_assignee_user_id'), table_name='flow_assignee')
    op.drop_table('flow_assignee')
    op.drop_table('flow_approval_target')
    op.drop_constraint(op.f("ck_channel_type"), "channel", type_="check")
    op.create_check_constraint(
        op.f("ck_channel_type"), "channel", "type IN ('talk', 'vote', 'doc', 'approval', 'work')"
    )
