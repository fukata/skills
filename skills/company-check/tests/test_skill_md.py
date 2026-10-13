"""SKILL.md が AGENTS.md の決まりと、手順どうしの参照を守っているかを確かめる。"""
import re
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEXT = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def frontmatter():
    match = re.match(r"---\n(.*?)\n---\n", TEXT, re.S)
    assert match, "frontmatter が無い"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


class SkillMdTest(unittest.TestCase):
    def test_frontmatter_keys(self):
        self.assertEqual(list(frontmatter()), ["name", "description", "license"])

    def test_name_matches_folder(self):
        self.assertEqual(frontmatter()["name"], SKILL_DIR.name)

    def test_license(self):
        self.assertEqual(frontmatter()["license"], "MIT")

    def test_description_length(self):
        self.assertLessEqual(len(frontmatter()["description"]), 1024)

    def test_no_agent_specific_placeholder(self):
        # $ARGUMENTS は Claude Code だけが置き換える
        self.assertNotIn("$ARGUMENTS", TEXT)

    def test_steps_are_numbered_in_order(self):
        numbers = [int(n) for n in re.findall(r"^### (\d+)\. ", TEXT, re.M)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_referenced_steps_exist(self):
        last = max(int(n) for n in re.findall(r"^### (\d+)\. ", TEXT, re.M))
        for ref in re.findall(r"手順(\d+)", TEXT):
            self.assertLessEqual(int(ref), last, f"手順{ref} は無い")

    def test_verdicts_are_defined(self):
        for verdict in ("懸念なし", "要確認", "避けるのが無難", "情報不足"):
            self.assertIn(f"- **{verdict}**:", TEXT)


if __name__ == "__main__":
    unittest.main()
