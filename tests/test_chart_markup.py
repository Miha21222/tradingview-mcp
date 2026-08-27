"""markup_json schema + validation tests (pure, no browser/network)."""

import pytest
from fastmcp.exceptions import ToolError

from tvmcp.chart.markup import Markup, parse_markup


def test_valid_markup_parses():
    m = parse_markup(
        '{"version":1,"grid":true,"markup":['
        '{"type":"fvg","time":"2026-08-01T06:00:00Z","direction":"bullish","top":1.1,"bottom":1.09},'
        '{"type":"ob","time":"2026-08-02T02:00:00Z","direction":"bearish","top":1.2,"bottom":1.19},'
        '{"type":"bos","time":"2026-08-03T08:00:00Z","level":1.13,"label":"BOS"},'
        '{"type":"choch","time":"2026-08-03T09:00:00Z","level":1.12},'
        '{"type":"killzone","start":"2026-08-02T06:00:00Z","end":"2026-08-02T09:00:00Z","label":"London"}]}'
    )
    assert m.version == 1
    assert len(m.markup) == 5
    assert m.grid is True


def test_empty_markup_is_empty():
    assert parse_markup("").markup == []
    assert parse_markup("   ").markup == []


def test_invalid_json_raises_toolerror():
    with pytest.raises(ToolError):
        parse_markup("{not json")


def test_unsupported_version_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":99,"markup":[]}')


def test_box_top_below_bottom_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"fvg","time":"2026-08-01T00:00:00Z","direction":"bullish","top":1.09,"bottom":1.10}]}')


def test_box_nonpositive_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"ob","time":"2026-08-01T00:00:00Z","direction":"bullish","top":0,"bottom":-1}]}')


def test_unknown_type_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"wibble","time":"2026-08-01T00:00:00Z"}]}')


def test_bad_direction_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"fvg","time":"2026-08-01T00:00:00Z","direction":"sideways","top":1.1,"bottom":1.09}]}')


def test_bad_level_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"bos","time":"2026-08-01T00:00:00Z","level":-2}]}')


def test_text_and_marker_parse():
    m = parse_markup(
        '{"version":1,"markup":['
        '{"type":"text","time":"2026-08-01T06:00:00Z","price":1.1,"text":"liquidity swept","color":"#ff9800"},'
        '{"type":"marker","time":"2026-08-01T07:00:00Z","price":1.09,"direction":"up","label":"entry"}]}'
    )
    assert len(m.markup) == 2
    assert m.markup[0].text == "liquidity swept"
    assert m.markup[1].direction == "up"


def test_colors_accepted_on_all_primitives():
    m = parse_markup(
        '{"version":1,"markup":['
        '{"type":"fvg","time":"2026-08-01T00:00:00Z","direction":"bullish","top":1.1,"bottom":1.09,"color":"#112233","label":"FVG"},'
        '{"type":"line","time":"2026-08-01T00:00:00Z","level":1.1,"color":"#AABBCC"},'
        '{"type":"killzone","start":"2026-08-01T06:00:00Z","end":"2026-08-01T09:00:00Z","color":"#00ff00"}]}'
    )
    assert [x.color for x in m.markup] == ["#112233", "#AABBCC", "#00ff00"]


def test_bad_color_raises():
    for bad in ('"red"', '"#12345"', '"#12345g"', '"123456"'):
        with pytest.raises(ToolError):
            parse_markup(f'{{"version":1,"markup":[{{"type":"line","time":"2026-08-01T00:00:00Z","level":1.1,"color":{bad}}}]}}')


def test_marker_bad_direction_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"marker","time":"2026-08-01T00:00:00Z","price":1.1,"direction":"sideways"}]}')


def test_text_empty_or_too_long_raises():
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"text","time":"2026-08-01T00:00:00Z","price":1.1,"text":""}]}')
    with pytest.raises(ToolError):
        parse_markup('{"version":1,"markup":[{"type":"text","time":"2026-08-01T00:00:00Z","price":1.1,"text":"' + "x" * 81 + '"}]}')


def test_markup_model_default():
    m = Markup()
    assert m.markup == []
    assert m.grid is True
