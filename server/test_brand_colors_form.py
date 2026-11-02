"""test_brand_colors_form.py — the brand colors picker: showBrandColorsForm
(a form tool of the admin's chat and the setup chat), its brandColorsForm
and the brandColorsPicked answer."""

import json
import re

import pytest

import chatForms
import chatModel
import chatTools
from conftest import BRAND_COLORS, assertExactShape, createTestStore, wipeCollections
from objects import BRAND_COLOR_SUGGESTIONS_MAX, ChatReplyResponse
from test_chat import calls, clock, frozenNow, model, offline, reply
from test_chat_components import (adminSays, formAnswerDetails,
                                  lastUserContent, signedUpAdmin)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'Conversations',
               'ChatTurns', 'ChatUsage']
ADMIN = 'bc-admin'


@pytest.fixture(autouse=True)
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def openPicker(model, suggestions, userId=ADMIN) -> dict:
    model(calls(('c1', 'showBrandColorsForm',
                 json.dumps({'suggestions': suggestions}))),
          reply("I've put a color picker below."))
    return adminSays('Change my brand colors', userId=userId)


def answer(res: dict, primary, secondary, userId=ADMIN) -> dict:
    [form] = res['components']
    return adminSays(None, res['conversationId'],
                     {'kind': 'brandColorsPicked', 'formId': form['formId'],
                      'primaryColor': primary, 'secondaryColor': secondary},
                     userId=userId)


# ------------------------------ the toolsets ------------------------------ #


def test_thePicker_isInTheAdminAndSetupChats_neverTheCustomers():
    assert chatForms.FORM_TOOLS['showBrandColorsForm'].toolsets == (
        'admin', chatForms.SETUP_TOOLSET)
    for toolset in ('admin', 'adminSetup'):
        names = {t['function']['name'] for t in chatTools.TOOLS[toolset]}
        assert 'showBrandColorsForm' in names
        assert 'showBrandColorsForm' in chatModel.CHAT_PROMPTS[toolset]
    assert 'showBrandColorsForm' not in chatForms.formToolNames('customer')
    assert chatForms.ANSWERS_TO['brandColorsPicked'] == 'brandColorsForm'


# ------------------------------- the form ------------------------------- #


def test_thePicker_startsAtTheStoresColors_withTheSuggestionsCleaned(model):
    createTestStore(ADMIN)
    suggestions = ['#2e7d32', ' #FFB300 ', '#2E7D32', 'green', '#12345',
                   '#000000', '#111111', '#222222', '#333333', '#444444']

    res = openPicker(model, suggestions)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form['kind'] == 'brandColorsForm' and form['formId']
    assert form['primaryColor'] == BRAND_COLORS['primaryColor']
    assert form['secondaryColor'] == BRAND_COLORS['secondaryColor']
    # Uppercase, each once, hex colors only, at most 6.
    assert form['suggestions'] == ['#2E7D32', '#FFB300', '#000000',
                                   '#111111', '#222222', '#333333']
    assert len(form['suggestions']) == BRAND_COLOR_SUGGESTIONS_MAX


def test_thePicker_takesNoSuggestions(model):
    createTestStore(ADMIN)
    model(calls(('c1', 'showBrandColorsForm', '{}')), reply('Below.'))

    [form] = adminSays('Colors', userId=ADMIN)['components']

    assert form['suggestions'] == []


def test_theSetupChat_opensThePicker_andItsAnswerGoesToTheModel(model,
                                                                 firebase):
    adminId = signedUpAdmin(firebase)

    res = openPicker(model, ['#2E7D32', '#FFB300'], userId=adminId)

    [form] = res['components']
    # A store not set up yet has no colors: the picker starts empty.
    assert (form['primaryColor'], form['secondaryColor']) == (None, None)
    assert form['suggestions'] == ['#2E7D32', '#FFB300']
    fake = model(reply('What is your store called?'))
    answer(res, '#2e7d32', '#FFB300', userId=adminId)
    content = lastUserContent(fake)
    assert content.startswith('Use #2E7D32 as the primary color and #FFB300 '
                              'as the secondary color\n\n')
    assert formAnswerDetails(content) == {
        'kind': 'brandColorsPicked', 'primaryColor': '#2E7D32',
        'secondaryColor': '#FFB300'}
    names = {t['function']['name']
             for t in fake.requests[-1]['tools']}
    assert 'createUserStore' in names


# ------------------------------ the answer ------------------------------ #


def test_theAnswer_takesAnyHexColor_notTheSuggestionsOnly(model):
    createTestStore(ADMIN)
    res = openPicker(model, ['#2E7D32'])

    fake = model(reply('Saved.'))
    answer(res, '#abcdef', '#123456')

    assert formAnswerDetails(lastUserContent(fake)) == {
        'kind': 'brandColorsPicked', 'primaryColor': '#ABCDEF',
        'secondaryColor': '#123456'}


def test_theAnswer_refusesTheSameColorTwice_andIsAnsweredOnce(model):
    createTestStore(ADMIN)
    res = openPicker(model, [])
    model()

    with pytest.raises(ValueError, match='^' + re.escape(
            chatForms.PICKED_TWICE) + '$'):
        answer(res, '#ABCDEF', '#abcdef')

    model(reply('Saved.'))
    answer(res, '#ABCDEF', '#123456')
    model()
    with pytest.raises(ValueError, match='^' + re.escape(
            chatForms.FORM_ALREADY_ANSWERED) + '$'):
        answer(res, '#ABCDEF', '#123456')


@pytest.mark.parametrize('primary, secondary, field', [
    (None, '#123456', 'primaryColor'),
    ('#ABCDEF', None, 'secondaryColor'),
    ('ABCDEF', '#123456', 'primaryColor'),
    ('#ABCDEF', '#12345G', 'secondaryColor'),
])
def test_theAnswer_needsBothHexColors(model, primary, secondary, field):
    createTestStore(ADMIN)
    res = openPicker(model, [])
    model()

    with pytest.raises(ValueError, match=re.escape(field)):
        answer(res, primary, secondary)


def test_theAnswer_doesNotAnswerTheSwatchCard(model):
    """A brandColorsPicked answer fits the picker only; the photo's swatch
    card keeps its own brandColors answer."""
    createTestStore(ADMIN)
    res = openPicker(model, [])
    [form] = res['components']
    model()

    with pytest.raises(ValueError, match='^' + re.escape(
            chatForms.FORM_DOES_NOT_MATCH) + '$'):
        adminSays(None, res['conversationId'],
                  {'kind': 'brandColors', 'formId': form['formId'],
                   'primaryColor': '#ABCDEF'}, userId=ADMIN)
