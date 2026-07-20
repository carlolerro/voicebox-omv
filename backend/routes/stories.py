"""Story endpoints."""

import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import database, models
from ..services import stories, story_rendering
from ..app import safe_content_disposition
from ..database import get_db

router = APIRouter()
ACTIVE_STORY_STATUSES = {"queued", "generating", "rendering"}


def _load_story_or_404(story_id: str, db: Session):
    story = db.query(database.Story).filter_by(id=story_id).first()
    if story is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return story


def _reject_active_story_mutation(story_id: str, db: Session):
    story = _load_story_or_404(story_id, db)
    if story.status in ACTIVE_STORY_STATUSES:
        raise HTTPException(status_code=409, detail="Story is currently processing")
    return story


def _invalidate_after_timeline_change(story, db: Session) -> None:
    story_rendering.invalidate_story_render(story, db)


def _download_filename(story) -> str:
    safe_name = "".join(
        c for c in story.name if c.isalnum() or c in (" ", "-", "_")
    ).strip()
    return f"{safe_name or 'story'}.wav"


@router.get("/stories", response_model=list[models.StoryResponse])
async def list_stories(db: Session = Depends(get_db)):
    """List all stories."""
    return await stories.list_stories(db)


@router.post("/stories", response_model=models.StoryResponse)
async def create_story(
    data: models.StoryCreate,
    db: Session = Depends(get_db),
):
    """Create a new story."""
    try:
        return await stories.create_story(data, db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stories/{story_id}", response_model=models.StoryDetailResponse)
async def get_story(
    story_id: str,
    db: Session = Depends(get_db),
):
    """Get a story with all its items."""
    story = await stories.get_story(story_id, db)
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    return story


@router.put("/stories/{story_id}", response_model=models.StoryResponse)
async def update_story(
    story_id: str,
    data: models.StoryCreate,
    db: Session = Depends(get_db),
):
    """Update a story."""
    _reject_active_story_mutation(story_id, db)
    story = await stories.update_story(story_id, data, db)
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    return story


@router.delete("/stories/{story_id}")
async def delete_story(
    story_id: str,
    db: Session = Depends(get_db),
):
    """Delete a terminal story, its workflow rows, and persisted render."""
    story = _reject_active_story_mutation(story_id, db)
    story_rendering.remove_persistent_render(story)
    db.query(database.StorySegment).filter_by(story_id=story_id).delete(
        synchronize_session=False
    )
    success = await stories.delete_story(story_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Story not found")
    return {"message": "Story deleted successfully"}


@router.post("/stories/{story_id}/items", response_model=models.StoryItemDetail)
async def add_story_item(
    story_id: str,
    data: models.StoryItemCreate,
    db: Session = Depends(get_db),
):
    """Add a generation to a story."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.add_item_to_story(story_id, data, db)
    if not item:
        raise HTTPException(status_code=404, detail="Story or generation not found")
    _invalidate_after_timeline_change(story, db)
    return item


@router.delete("/stories/{story_id}/items/{item_id}")
async def remove_story_item(
    story_id: str,
    item_id: str,
    db: Session = Depends(get_db),
):
    """Remove a story item from a story."""
    story = _reject_active_story_mutation(story_id, db)
    success = await stories.remove_item_from_story(story_id, item_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Story item not found")
    _invalidate_after_timeline_change(story, db)
    return {"message": "Item removed successfully"}


@router.put("/stories/{story_id}/items/times")
async def update_story_item_times(
    story_id: str,
    data: models.StoryItemBatchUpdate,
    db: Session = Depends(get_db),
):
    """Update story item timecodes."""
    story = _reject_active_story_mutation(story_id, db)
    success = await stories.update_story_item_times(story_id, data, db)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid timecode update request")
    _invalidate_after_timeline_change(story, db)
    return {"message": "Item timecodes updated successfully"}


@router.put("/stories/{story_id}/items/reorder", response_model=list[models.StoryItemDetail])
async def reorder_story_items(
    story_id: str,
    data: models.StoryItemReorder,
    db: Session = Depends(get_db),
):
    """Reorder story items and recalculate timecodes."""
    story = _reject_active_story_mutation(story_id, db)
    items = await stories.reorder_story_items(story_id, data.generation_ids, db)
    if items is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid reorder request - ensure all generation IDs belong to this story",
        )
    _invalidate_after_timeline_change(story, db)
    return items


@router.put("/stories/{story_id}/items/{item_id}/move", response_model=models.StoryItemDetail)
async def move_story_item(
    story_id: str,
    item_id: str,
    data: models.StoryItemMove,
    db: Session = Depends(get_db),
):
    """Move a story item (update position and/or track)."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.move_story_item(story_id, item_id, data, db)
    if item is None:
        raise HTTPException(status_code=404, detail="Story item not found")
    _invalidate_after_timeline_change(story, db)
    return item


@router.put("/stories/{story_id}/items/{item_id}/trim", response_model=models.StoryItemDetail)
async def trim_story_item(
    story_id: str,
    item_id: str,
    data: models.StoryItemTrim,
    db: Session = Depends(get_db),
):
    """Trim a story item."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.trim_story_item(story_id, item_id, data, db)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="Story item not found or invalid trim values",
        )
    _invalidate_after_timeline_change(story, db)
    return item


@router.put("/stories/{story_id}/items/{item_id}/volume", response_model=models.StoryItemDetail)
async def update_story_item_volume(
    story_id: str,
    item_id: str,
    data: models.StoryItemVolumeUpdate,
    db: Session = Depends(get_db),
):
    """Set a story item's per-clip volume (linear gain, 0.0–2.0)."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.update_story_item_volume(story_id, item_id, data, db)
    if item is None:
        raise HTTPException(status_code=404, detail="Story item not found")
    _invalidate_after_timeline_change(story, db)
    return item


@router.post("/stories/{story_id}/items/{item_id}/split", response_model=list[models.StoryItemDetail])
async def split_story_item(
    story_id: str,
    item_id: str,
    data: models.StoryItemSplit,
    db: Session = Depends(get_db),
):
    """Split a story item at a given time, creating two clips."""
    story = _reject_active_story_mutation(story_id, db)
    items = await stories.split_story_item(story_id, item_id, data, db)
    if items is None:
        raise HTTPException(
            status_code=404,
            detail="Story item not found or invalid split point",
        )
    _invalidate_after_timeline_change(story, db)
    return items


@router.post("/stories/{story_id}/items/{item_id}/duplicate", response_model=models.StoryItemDetail)
async def duplicate_story_item(
    story_id: str,
    item_id: str,
    db: Session = Depends(get_db),
):
    """Duplicate a story item."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.duplicate_story_item(story_id, item_id, db)
    if item is None:
        raise HTTPException(status_code=404, detail="Story item not found")
    _invalidate_after_timeline_change(story, db)
    return item


@router.put("/stories/{story_id}/items/{item_id}/version", response_model=models.StoryItemDetail)
async def set_story_item_version(
    story_id: str,
    item_id: str,
    data: models.StoryItemVersionUpdate,
    db: Session = Depends(get_db),
):
    """Pin a story item to a specific generation version."""
    story = _reject_active_story_mutation(story_id, db)
    item = await stories.set_story_item_version(story_id, item_id, data, db)
    if item is None:
        raise HTTPException(status_code=404, detail="Story item or version not found")
    _invalidate_after_timeline_change(story, db)
    return item


@router.get("/stories/{story_id}/export-audio")
async def export_story_audio(
    story_id: str,
    db: Session = Depends(get_db),
):
    """Serve a persisted Story render or fall back to legacy on-demand mixing."""
    try:
        story = _load_story_or_404(story_id, db)
        filename = _download_filename(story)
        headers = {
            "Content-Disposition": safe_content_disposition("attachment", filename)
        }

        persisted = story_rendering.resolve_valid_render_path(story)
        if persisted is not None:
            return FileResponse(
                str(persisted),
                media_type="audio/wav",
                headers=headers,
            )

        audio_bytes = await stories.export_story_audio(story_id, db)
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Story has no audio items")

        return StreamingResponse(
            io.BytesIO(audio_bytes),
            media_type="audio/wav",
            headers=headers,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
