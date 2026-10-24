from __future__ import annotations

import io
import json

import pytest
from PIL import Image
from playwright.sync_api import expect


def payload(uuid='sermon', notes='Sermon notes'):
    return {'presentation': {'id': {'uuid': uuid, 'name': uuid}, 'groups': [{'slides': [{'enabled': True, 'text': '', 'notes': notes}]}]}, 'slide_index': {'presentation_index': {'presentation_id': {'uuid': uuid}, 'index': 0}}}


def image_bytes():
    b = io.BytesIO()
    Image.new('RGB', (220, 124), 'blue').save(b, format='PNG')
    return b.getvalue()


@pytest.mark.browser
def test_clicker_keeps_notes_when_snapshot_identity_disagrees(page, safe_web_base_url):
    state = payload()
    page.route('**/active-presentation', lambda route: route.fulfill(json=state))
    page.route('**/thumbnail?*', lambda route: route.fulfill(body=image_bytes(), content_type='image/png'))
    page.goto(safe_web_base_url+'/index.html')
    expect(page.locator('#presentationName')).to_have_text('sermon')
    state = payload('wrong-song', 'Wrong notes')
    state['slide_index']['presentation_index']['presentation_id']['uuid'] = 'sermon'
    page.evaluate('fetchStatus()')
    expect(page.locator('#presentationName')).to_have_text('sermon')
    assert 'Wrong notes' not in page.locator('body').inner_text()


@pytest.mark.browser
def test_clicker_retries_initial_thumbnail_failure_and_invalid_image(page, safe_web_base_url):
    attempts = []
    def thumbnail(route):
        attempts.append(1)
        if len(attempts) == 1:
            route.fulfill(status=503, body='retry')
        elif len(attempts) == 2:
            route.fulfill(body=b'not a PNG'*30, content_type='image/png')
        else:
            route.fulfill(body=image_bytes(), content_type='image/png')
    page.route('**/active-presentation', lambda route: route.fulfill(json=payload()))
    page.route('**/thumbnail?*', thumbnail)
    page.goto(safe_web_base_url+'/index.html')
    page.wait_for_function("document.querySelector('.slide-thumb')?.naturalWidth === 220", timeout=12000)
    assert len(attempts) >= 3


@pytest.mark.browser
def test_slow_status_requests_do_not_starve_rendering_or_overlap(page, safe_web_base_url):
    page.route('**/active-presentation', lambda route: route.fulfill(json=payload()))
    page.route('**/thumbnail?*', lambda route: route.fulfill(body=image_bytes(), content_type='image/png'))
    page.goto(safe_web_base_url+'/index.html')
    expect(page.locator('#presentationName')).to_have_text('sermon')
    result = page.evaluate("""async () => {
        const original = fetchJson;
        let active = 0, peak = 0, count = 0;
        fetchJson = async (...args) => {
            if (!args[0].endsWith('/active-presentation')) return original(...args);
            active++; count++; peak = Math.max(peak, active);
            await new Promise(resolve => setTimeout(resolve, 1300));
            const data = await original(...args);
            active--;
            return data;
        };
        await Promise.all([fetchStatus(), fetchStatus(), fetchStatus()]);
        fetchJson = original;
        return {peak, count};
    }""")
    assert result == {'peak': 1, 'count': 1}
    expect(page.locator('#presentationName')).to_have_text('sermon')


@pytest.mark.browser
def test_live_playlist_without_slide_number_is_not_labelled_inactive(page, safe_web_base_url):
    state = payload()
    state['slide_index'] = {'presentation_index': None}
    state['presentation_mode'] = 'live'
    page.route('**/active-presentation', lambda route: route.fulfill(json=state))
    page.route('**/thumbnail?*', lambda route: route.fulfill(body=image_bytes(), content_type='image/png'))
    page.goto(safe_web_base_url+'/index.html')
    expect(page.locator('#presentationName')).to_have_text('sermon')
    expect(page.locator('#statusText')).to_have_text('Live · Slide position unavailable')
