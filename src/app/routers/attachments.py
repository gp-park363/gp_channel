from datetime import UTC, datetime
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import func, select

from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.models import Attachment
from app.routers.posts import card, editable_view, is_document, post_context
from app.services.audit import record_audit
from app.services.realtime import Event, publish
from app.services.storage import content_disposition, open_file

router = APIRouter(
    tags=["attachments"], include_in_schema=False, dependencies=[Depends(current_user)]
)


@router.get("/attachments/{attachment_id}")
async def download(
    attachment_id: int,
    session: SessionDep,
    authz: AuthzDep,
    download: Annotated[bool, Query()] = False,
) -> Response:
    """글을 볼 수 있는지 확인한 뒤에만 앱이 스토리지에서 받아 그대로 흘려보낸다."""
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다.")
    post, *_ = await post_context(session, authz, attachment.post_id)
    if post.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다.")
    body = await to_thread.run_sync(open_file, attachment.storage_key)

    async def chunks():
        try:
            while chunk := await to_thread.run_sync(body.read, 64 * 1024):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(
        chunks(),
        media_type=attachment.mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": content_disposition(
                attachment.file_name, inline=attachment.is_image and not download
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/attachments/{attachment_id}/delete", response_class=HTMLResponse)
async def delete_attachment(
    attachment_id: int, session: SessionDep, authz: AuthzDep
) -> HTMLResponse:
    """첨부는 지우기만 할 수 있다. 새 파일은 쓰레드 답글로 올린다."""
    attachment = await session.get(Attachment, attachment_id, with_for_update=True)
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다.")
    post, parent, channel, access = await editable_view(
        session, authz, attachment.post_id, lock=True
    )
    if is_document(post, channel):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "문서의 첨부는 지울 수 없습니다.")
    remaining = await session.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.post_id == post.id,
            Attachment.id != attachment.id,
            Attachment.deleted_at.is_(None),
        )
    )
    if not remaining and not post.body_md.strip():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "본문이 없는 글의 마지막 첨부입니다. 본문을 먼저 쓰거나 글을 삭제해 주세요.",
        )
    now = datetime.now(UTC)
    attachment.deleted_at = now
    attachment.deleted_by = authz.user.id
    post.edited_at = now
    record_audit(
        session,
        actor_id=authz.user.id,
        action="attachment.delete",
        target_type="attachment",
        target_id=attachment.id,
        post_id=post.id,
        channel_id=channel.id,
        file_name=attachment.file_name,
    )
    await publish(session, Event("updated", channel.id, post.id, authz.user.id))
    await session.commit()
    return await card(session, authz, post, parent, channel, access)
