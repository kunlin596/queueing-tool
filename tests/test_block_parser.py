"""Block-header parsing — the contract every job script relies on."""

from queueing_tool.block_parser import Block_Parser


def _write_script(tmp_path, header):
    script = tmp_path / "job.sh"
    script.write_text(f"{header}\necho hello\n")
    return str(script)


def test_parses_all_block_fields(tmp_path):
    script = _write_script(
        tmp_path,
        "#block(name=prelim1pct_abl6, threads=8, memory=20000, subtasks=1, gpus=1, hours=6)",
    )
    blocks = Block_Parser().parse(script)
    assert len(blocks) == 1
    v = blocks[0].values
    assert v["name"] == "prelim1pct_abl6"
    assert v["threads"] == 8
    assert v["memory"] == 20000
    assert v["subtasks"] == 1
    assert v["gpus"] == 1
    assert v["hours"] == 6
    assert any("echo hello" in line for line in v["script"])


def test_multiple_blocks_split_on_headers(tmp_path):
    script = tmp_path / "multi.sh"
    script.write_text(
        "#block(name=a, threads=1, memory=100, subtasks=1, gpus=0, hours=1)\n"
        "echo a\n"
        "#block(name=b, threads=2, memory=200, subtasks=1, gpus=0, hours=1)\n"
        "echo b\n"
    )
    blocks = Block_Parser().parse(str(script))
    assert [b.values["name"] for b in blocks] == ["a", "b"]
