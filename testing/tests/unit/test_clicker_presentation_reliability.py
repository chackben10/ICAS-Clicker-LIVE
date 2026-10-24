from __future__ import annotations

import asyncio
import io
import unittest
from unittest.mock import AsyncMock

from PIL import Image

from production_hub.core.config.models import ProPresenterConfig, ThumbnailConfig
from production_hub.integrations.propresenter.client import ProPresenterHttpError
from production_hub.integrations.propresenter.service import ProPresenterService
from production_hub.integrations.propresenter.thumbnail_cache import ThumbnailCache, TRANSPARENT_PNG


def document(uuid, slides=5, name=None):
    return {'presentation': {'id': {'uuid': uuid, 'name': uuid if name is None else name},
                             'groups': [{'slides': [{'notes': uuid} for _ in range(slides)]}]}}


def position(uuid, index=0):
    return {'presentation_index': {'presentation_id': {'uuid': uuid}, 'index': index}}


def png():
    output = io.BytesIO()
    Image.new('RGB', (20, 20), 'white').save(output, format='PNG')
    return output.getvalue()


class PresentationReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = ProPresenterService(ProPresenterConfig())
        self.service._active_playlist_uuid = AsyncMock(return_value="")

    async def test_live_playlist_preserves_sermon_when_slide_index_is_missing(self):
        self.service.slide_index = AsyncMock(return_value={'presentation_index': None})
        self.service._active_playlist_uuid = AsyncMock(return_value='sermon')
        self.service.active_presentation = AsyncMock(return_value=document('sermon'))
        self.service.focused_presentation = AsyncMock(return_value={'uuid': 'wrong-song'})
        result = await self.service.full_presentation()
        self.assertEqual('sermon', result['presentation']['id']['uuid'])
        self.assertEqual('live', result['presentation_mode'])
        self.assertIsNone(result['slide_index']['presentation_index'])
        self.service.focused_presentation.assert_not_awaited()

    async def test_missing_slide_index_controls_still_target_live_playlist(self):
        self.service.slide_index = AsyncMock(return_value={'presentation_index': None})
        self.service.monitored_slide_index = self.service.slide_index
        self.service._active_playlist_uuid = AsyncMock(return_value='sermon')
        self.service.client.trigger = AsyncMock(return_value=True)
        await self.service.focus_slide(4)
        await self.service.next_slide()
        self.assertEqual(['/presentation/active/4/trigger', '/presentation/active/next/trigger'],
                         [call.args[0] for call in self.service.client.trigger.await_args_list])

    async def test_cleared_playlist_does_not_reuse_stale_active_document(self):
        self.service.slide_index = AsyncMock(return_value={'presentation_index': None})
        self.service._focused_uuid = AsyncMock(return_value='selected-song')
        self.service.presentation_by_uuid = AsyncMock(return_value=document('selected-song'))
        self.service.active_presentation = AsyncMock(return_value=document('stale-sermon'))
        result = await self.service.full_presentation()
        self.assertEqual('focused', result['presentation_mode'])
        self.assertEqual('selected-song', result['presentation']['id']['uuid'])

    async def test_failed_status_does_not_switch_to_focused_notes(self):
        self.service.slide_index = AsyncMock(side_effect=OSError('timeout'))
        self.service.focused_presentation = AsyncMock()
        with self.assertRaises(OSError):
            await self.service.full_presentation()
        self.service.focused_presentation.assert_not_awaited()

    async def test_stale_active_document_is_resolved_using_live_identity(self):
        self.service.slide_index = AsyncMock(return_value=position('sermon', 4))
        self.service.active_presentation = AsyncMock(return_value=document('song'))
        self.service.presentation_by_uuid = AsyncMock(return_value=document('sermon'))
        result = await self.service.full_presentation()
        self.assertEqual('sermon', result['presentation']['id']['uuid'])
        self.assertEqual(position('sermon', 4), result['slide_index'])

    async def test_previous_documents_position_is_not_applied_to_new_document(self):
        stale = position('fully-known', 4)
        stale['presentation_index']['total_cues'] = 38
        self.service.slide_index = AsyncMock(return_value=stale)
        self.service.active_presentation = AsyncMock(return_value=document('fully-known', slides=16))
        result = await self.service.full_presentation()
        self.assertEqual('live', result['presentation_mode'])
        self.assertIsNone(result['slide_index']['presentation_index'])
        self.assertEqual(16, len(result['presentation']['groups'][0]['slides']))

    async def test_position_past_the_last_slide_is_not_reported(self):
        self.service.slide_index = AsyncMock(return_value=position('song', 9))
        self.service.active_presentation = AsyncMock(return_value=document('song', slides=3))
        result = await self.service.full_presentation()
        self.assertIsNone(result['slide_index']['presentation_index'])

    async def test_blank_live_name_is_taken_from_the_playlist_entry(self):
        self.service.slide_index = AsyncMock(return_value=position('fully-known', 1))
        self.service.active_presentation = AsyncMock(return_value=document('fully-known', name=''))
        self.service.client.get_json = AsyncMock(return_value={'presentation': {'playlist_item': {
            'id': {'name': 'Fully Known Presentation'},
            'presentation_info': {'presentation_uuid': 'FULLY-KNOWN'}}}})
        del self.service._active_playlist_uuid
        result = await self.service.full_presentation()
        self.assertEqual('Fully Known Presentation', result['presentation']['id']['name'])
        self.assertEqual(1, result['slide_index']['presentation_index']['index'])

    async def test_cue_change_during_read_retries_entire_snapshot(self):
        self.service.slide_index = AsyncMock(side_effect=[position('song'), position('sermon'), position('sermon'), position('sermon')])
        self.service.active_presentation = AsyncMock(side_effect=[document('song'), document('sermon')])
        result = await self.service.full_presentation()
        self.assertEqual('sermon', result['presentation']['id']['uuid'])

    async def test_uuid_missing_in_pp22_index_accepts_only_matching_current_document(self):
        self.service.client.get_json = AsyncMock(side_effect=[ProPresenterHttpError(404, 'missing'), document('sermon')])
        self.assertEqual(document('sermon'), await self.service.presentation_by_uuid('sermon'))
        self.service.client.get_json = AsyncMock(side_effect=[ProPresenterHttpError(404, 'missing'), document('song')])
        with self.assertRaises(ProPresenterHttpError):
            await self.service.presentation_by_uuid('sermon')

    async def test_wrong_uuid_success_response_is_rejected(self):
        self.service.client.get_json = AsyncMock(return_value=document('song'))
        with self.assertRaises(ValueError):
            await self.service.presentation_by_uuid('sermon')

    async def test_focused_fallback_is_still_available_without_live_cue(self):
        self.service.slide_index = AsyncMock(return_value={'presentation_index': None})
        self.service._focused_uuid = AsyncMock(return_value='sermon')
        self.service.presentation_by_uuid = AsyncMock(return_value=document('sermon'))
        result = await self.service.full_presentation()
        self.assertEqual(document('sermon')['presentation'], result['presentation'])
        self.assertIsNone(result['slide_index']['presentation_index'])

    async def test_focused_announcements_do_not_mutate_propresenter(self):
        self.service.slide_index = AsyncMock(return_value={'presentation_index': None})
        self.service._focused_uuid = AsyncMock(return_value='announcement')
        payload = document('announcement')
        payload['presentation']['destination'] = 'announcements'
        self.service.presentation_by_uuid = AsyncMock(return_value=payload)
        self.service.client.trigger = AsyncMock()
        result = await self.service.full_presentation()
        self.assertIsNone(result['presentation'])
        self.service.client.trigger.assert_not_awaited()


class ThumbnailReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_images_and_placeholders_are_not_cached(self):
        service = ProPresenterService(ProPresenterConfig())
        for invalid in (b'', b'{"error":"not ready"}', TRANSPARENT_PNG, png()[:30]):
            service.client.get_bytes = AsyncMock(side_effect=[invalid, png()])
            cache = ThumbnailCache(service.client, ThumbnailConfig())
            self.assertEqual(TRANSPARENT_PNG, (await cache.fetch('sermon', 0)).body)
            self.assertIsNone(cache.get('sermon', 0))
            self.assertEqual(png(), (await cache.fetch('sermon', 0)).body)

    async def test_simultaneous_clients_share_one_thumbnail_request(self):
        service = ProPresenterService(ProPresenterConfig())
        service.client.get_bytes = AsyncMock(return_value=png())
        results = await asyncio.gather(*(service.thumbnails.fetch('sermon', 0) for _ in range(12)))
        self.assertTrue(all(result.body == png() for result in results))
        service.client.get_bytes.assert_awaited_once()

    async def test_thumbnail_fanout_is_bounded(self):
        service = ProPresenterService(ProPresenterConfig())
        active = peak = 0
        async def get_bytes(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            return png()
        service.client.get_bytes = get_bytes
        await asyncio.gather(*(service.thumbnails.fetch('sermon', index) for index in range(16)))
        self.assertEqual(3, peak)

    async def test_transient_failure_keeps_last_good_image_without_refreshing_its_age(self):
        service = ProPresenterService(ProPresenterConfig())
        entry = service.thumbnails.put('sermon', 0, png(), 'image/png', 'low')
        entry.stored_at -= 30
        original_age = entry.stored_at
        service.client.get_bytes = AsyncMock(side_effect=OSError('timeout'))
        self.assertIs(entry, await service.thumbnails.fetch('sermon', 0))
        self.assertEqual(original_age, entry.stored_at)
