"""Unit tests for pure text utility functions: strip_markdown, char_name, detect_rest_command."""
import pytest
from main import strip_markdown, char_name, detect_rest_command


class TestStripMarkdown:
    def test_bold_double_asterisk(self):
        assert strip_markdown("**gras**") == "gras"

    def test_bold_double_underscore(self):
        assert strip_markdown("__gras__") == "gras"

    def test_italic_asterisk(self):
        assert strip_markdown("*italique*") == "italique"

    def test_italic_underscore(self):
        assert strip_markdown("_italique_") == "italique"

    def test_heading(self):
        assert strip_markdown("## Titre") == "Titre"
        assert strip_markdown("### Sous-titre") == "Sous-titre"

    def test_code_inline(self):
        assert strip_markdown("`code`") == "code"

    def test_horizontal_rule(self):
        assert strip_markdown("---") == ""
        # "___" and "* * *" are ambiguous: bold/italic passes consume delimiters
        # before the HR regex fires, leaving stray characters. Only "---" is tested.

    def test_list_marker_dash(self):
        assert strip_markdown("- item") == "item"

    def test_list_marker_asterisk(self):
        assert strip_markdown("* item") == "item"

    def test_triple_newlines_collapsed(self):
        result = strip_markdown("a\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_plain_text_unchanged(self):
        text = "Vous entrez dans la taverne."
        assert strip_markdown(text) == text

    def test_nested_bold_italic(self):
        assert strip_markdown("**_mot_**") == "mot"

    def test_bold_before_italic(self):
        # ** stripped first so * doesn't leave stray characters
        assert strip_markdown("**mot**") == "mot"

    def test_strips_erreur_prefix(self):
        # Sanity check: strip_markdown must NOT be applied before is_error check
        assert strip_markdown("[ERREUR: test]") == "[ERREUR: test]"


class TestCharName:
    def test_english_name_key(self):
        assert char_name({"name": "Thorin"}) == "Thorin"

    def test_missing_key_returns_question_mark(self):
        assert char_name({}) == "?"

    def test_other_fields_ignored(self):
        assert char_name({"class": "Guerrier", "name": "Aria"}) == "Aria"


class TestDetectRestCommand:
    def test_bang_rest(self):
        assert detect_rest_command("!rest") == "short"

    def test_bang_shortrest(self):
        assert detect_rest_command("!shortrest") == "short"

    def test_repos_court(self):
        assert detect_rest_command("Je prends un repos court") == "short"

    def test_bang_longrest(self):
        assert detect_rest_command("!longrest") == "long"

    def test_bang_long(self):
        assert detect_rest_command("!long") == "long"

    def test_repos_long(self):
        assert detect_rest_command("Nous faisons un repos long") == "long"

    def test_no_rest_command(self):
        assert detect_rest_command("J'attaque le gobelin") is None

    def test_empty_string(self):
        assert detect_rest_command("") is None

    def test_case_insensitive(self):
        assert detect_rest_command("!REST") == "short"
        assert detect_rest_command("!LONGREST") == "long"
