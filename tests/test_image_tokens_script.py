"""scripts/vllm/image_tokens.py: the offline vision-token table. Pure arithmetic
on a processor's size settings -- no model, weights, torch or server."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from levi.agent.schema import MAX_IMAGE_PIXELS, MIN_IMAGE_PIXELS
from levi.inference.provider import encode_image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vllm" / "image_tokens.py"
spec = importlib.util.spec_from_file_location("image_tokens_under_test", SCRIPT)
tokens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tokens)

# The image processor of the Qwen3.8 27B snapshot, as its processor_config.json
# states it (the settings only, no weights).
QWEN3 = {
    "image_processor": {
        "image_processor_type": "Qwen2VLImageProcessor",
        "merge_size": 2,
        "patch_size": 16,
        "size": {"longest_edge": 16777216, "shortest_edge": 65536},
        "temporal_patch_size": 2,
    },
    "processor_class": "Qwen3VLProcessor",
}


@pytest.fixture
def qwen3(tmp_path):
    folder = tmp_path / "snapshot"
    folder.mkdir()
    (folder / "processor_config.json").write_text(json.dumps(QWEN3))
    return folder


def by_setting(table):
    return {(row["route"], row["value"]): row for row in table["rows"]}


def test_a_640x480_frame_costs_300_tokens_and_a_448_copy_140_not_154(qwen3):
    table = tokens.build_table(tokens.load_processor(qwen3), 640, 480, (448,))
    rows = by_setting(table)
    assert rows[("native", None)]["tokens"] == 300
    # 640x480 is 20 x 15 blocks of 32 px. At 448 the copy is 448x336, and
    # 336 / 32 = 10.5: Python's round() (which the processor uses) sends an
    # exact half to the even neighbour, 10, so the processor sees 448x320 =
    # 14 x 10 blocks. Rounding the half up gives 11 x 14 = 154, the figure a
    # back-of-the-envelope estimate arrives at.
    assert rows[("image_max_side", 448)]["sent"] == [448, 336]
    assert rows[("image_max_side", 448)]["processed"] == [448, 320]
    assert rows[("image_max_side", 448)]["tokens"] == 140


def test_the_table_for_a_640x480_frame_under_the_qwen3_processor(qwen3):
    table = tokens.build_table(
        tokens.load_processor(qwen3), 640, 480, (448, 384, 320, 256)
    )
    rows = by_setting(table)
    # (sent, processed, tokens), as the Hugging Face image processor of the
    # same settings returned them for each row (transformers 5.17, CPU).
    expected = {
        ("native", None): ([640, 480], [640, 480], 300),
        ("image_max_side", 448): ([448, 336], [448, 320], 140),
        ("image_max_side", 384): ([384, 288], [384, 288], 108),
        ("image_max_side", 320): ([320, 240], [320, 256], 80),
        # 256x192 is under the 65,536 px floor: it is enlarged, so a smaller
        # copy does not cost fewer tokens than this.
        ("image_max_side", 256): ([256, 192], [320, 224], 70),
        ("max_pixels", 150528): ([640, 480], [448, 320], 140),
        ("max_pixels", 110592): ([640, 480], [384, 288], 108),
        ("max_pixels", 76800): ([640, 480], [320, 224], 70),
        ("max_pixels", 49152): ([640, 480], [256, 192], 48),
    }
    assert set(rows) == set(expected)
    for key, (sent, processed, count) in expected.items():
        assert (rows[key]["sent"], rows[key]["processed"], rows[key]["tokens"]) == (
            sent,
            processed,
            count,
        ), key
    assert rows[("native", None)]["relative"] == 1.0
    assert rows[("image_max_side", 448)]["relative"] == 0.467
    assert "enlarges it to 320x224" in rows[("image_max_side", 256)]["note"]
    # A budget below the floor is what the profile field refuses.
    assert "profile field refuses" in rows[("max_pixels", 49152)]["note"]
    assert rows[("max_pixels", 150528)]["note"] == ""


def test_a_pixel_budget_is_never_exceeded_where_the_image_is_larger(qwen3):
    processor = tokens.load_processor(qwen3)
    for width, height in ((640, 480), (1280, 720), (1000, 333), (1920, 1080)):
        for budget in (65536, 100000, 150528, 262144, 1048576):
            if width * height <= budget:
                continue
            result = tokens.image_tokens(width, height, processor, budget)
            area = result["processed"][0] * result["processed"][1]
            assert area <= budget, (width, height, budget)
            assert result["tokens"] == area // 1024


@pytest.mark.parametrize("size", [(640, 480), (1000, 333), (97, 113)])
@pytest.mark.parametrize("side", [448, 384, 320, 300, 256, 4000])
def test_the_scaled_copy_is_the_size_encode_image_sends(tmp_path, size, side):
    width, height = size
    path = tmp_path / "frame.png"
    assert cv2.imwrite(str(path), np.zeros((height, width, 3), np.uint8))
    sent = base64.b64decode(encode_image(path, SimpleNamespace(image_max_side=side)))
    shape = cv2.imdecode(np.frombuffer(sent, np.uint8), cv2.IMREAD_COLOR).shape[:2]
    assert shape == tuple(reversed(tokens.scaled_size(width, height, side)))
    assert tokens.scaled_size(width, height, None) == (width, height)


def test_the_profiles_pixel_range_is_the_processors_floor_and_ceiling(qwen3):
    processor = tokens.load_processor(qwen3)
    assert processor["min_pixels"] == MIN_IMAGE_PIXELS == 65536
    assert processor["max_pixels"] == MAX_IMAGE_PIXELS == 16777216
    assert processor["factor"] == 32


def test_the_settings_load_from_either_layout_or_the_file_itself(tmp_path, qwen3):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "patch_size": 14,
                "merge_size": 2,
                "min_pixels": 3136,
                "max_pixels": 1003520,
            }
        )
    )
    assert tokens.load_processor(flat) == {
        "source": "preprocessor_config.json",
        "patch_size": 14,
        "merge_size": 2,
        "min_pixels": 3136,
        "max_pixels": 1003520,
        "factor": 28,
    }
    assert tokens.load_processor(qwen3 / "processor_config.json")["factor"] == 32
    assert tokens.load_processor(qwen3)["source"] == "processor_config.json"


def test_unusable_settings_are_named_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="no processor_config.json"):
        tokens.load_processor(tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    image = {**QWEN3["image_processor"]}
    del image["patch_size"]
    (broken / "processor_config.json").write_text(
        json.dumps({"image_processor": image})
    )
    with pytest.raises(ValueError, match="`patch_size`"):
        tokens.load_processor(broken)
    image = {
        **QWEN3["image_processor"],
        "size": {"longest_edge": 100, "shortest_edge": 200},
    }
    (broken / "processor_config.json").write_text(
        json.dumps({"image_processor": image})
    )
    with pytest.raises(ValueError, match="min_pixels exceeds max_pixels"):
        tokens.load_processor(broken)


def test_a_ribbon_is_refused_as_the_processor_refuses_it(qwen3):
    with pytest.raises(ValueError, match="aspect ratio"):
        tokens.image_tokens(4000, 10, tokens.load_processor(qwen3))


def test_the_command_writes_both_tables_and_names_a_bad_input(tmp_path, qwen3, capsys):
    markdown, data = tmp_path / "table.md", tmp_path / "table.json"
    assert (
        tokens.main([str(qwen3), "--markdown", str(markdown), "--json", str(data)]) == 0
    )
    assert capsys.readouterr().out == ""
    text = markdown.read_text()
    assert "| native |  | 640x480 | 640x480 | 300 | 1.00 |  |" in text
    assert "| image_max_side | 448 | 448x336 | 448x320 | 140 | 0.47 |" in text
    loaded = json.loads(data.read_text())
    assert loaded["image"] == {"width": 640, "height": 480}
    assert loaded["processor"]["factor"] == 32
    assert [row["tokens"] for row in loaded["rows"]][:5] == [300, 140, 108, 80, 70]
    # Without a file argument the table goes to the terminal.
    assert tokens.main([str(qwen3), "--image", "1280x720", "--sides", "640"]) == 0
    assert "| native |  | 1280x720 | 1280x704 | 880 | 1.00 |" in capsys.readouterr().out
    assert tokens.main([str(tmp_path / "missing")]) == 2
    assert "image_tokens: no processor_config.json" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        tokens.main([str(qwen3), "--image", "640"])
