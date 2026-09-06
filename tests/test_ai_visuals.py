"""AI image provider and the character bible.

No network: the backend is an interface, so every test here drives a fake one.
That is deliberate - the real endpoint takes 8 to 45 seconds per image and is
rate limited, so a suite that called it would be both slow and flaky.
"""
from __future__ import annotations

import io
import random

import pytest
from PIL import Image

from engine.content.characters import Character, CharacterBible, build_bible
from engine.core.config import load_config
from engine.visuals.ai_image import (AIImageProvider, HuggingFaceBackend,
                                     PollinationsBackend, average_hash,
                                     build_backend, hamming)
from engine.visuals.base import VisualRequest


def _png_bytes(colour, size=(128, 128), noise: int = 0) -> bytes:
    """A distinguishable image, big enough to be accepted.

    is_valid_image() rejects anything under 4 kB. A flat-colour PNG compresses
    to a few hundred bytes, and so does a periodic gradient - both earlier
    versions of this helper produced files the provider correctly discarded, so
    every test failed for a reason unrelated to what it was testing. The
    content is therefore deterministic pseudo-random noise: incompressible, but
    identical for identical arguments, which the duplicate tests rely on.
    """
    rng = random.Random(repr((tuple(colour), noise, tuple(size))))
    img = Image.new("RGB", size)
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeBackend:
    id = "fake"
    label = "fake"
    model = "fake-model"

    def __init__(self, images):
        self.images = list(images)
        self.calls: list[tuple[str, int]] = []

    def available(self) -> bool:
        return True

    def fetch(self, prompt, *, width, height, seed):
        self.calls.append((prompt, seed))
        if not self.images:
            raise RuntimeError("fake: exhausted")
        item = self.images.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _req(index=0, **kw):
    base = dict(scene_index=index, prompt="a village path after rain",
                keywords=["village"], width=64, height=64,
                style="2D illustration, flat colours")
    base.update(kw)
    return VisualRequest(**base)


# ==========================================================================
class TestPromptShape:
    """The scene has to lead; see note 2 in the provider's module docstring."""

    def test_the_scene_comes_first(self):
        p = AIImageProvider(FakeBackend([]))
        prompt = p.build_prompt(_req(characters="RAJU is a boy in a green shirt"))
        assert prompt.startswith("a village path after rain")

    def test_characters_precede_the_style(self):
        p = AIImageProvider(FakeBackend([]))
        prompt = p.build_prompt(_req(characters="RAJU is a boy"))
        assert prompt.index("RAJU") < prompt.index("2D illustration")

    def test_nothing_contradicts_the_requested_style(self):
        """The old path appended 'professional photography' to everything."""
        p = AIImageProvider(FakeBackend([]))
        prompt = p.build_prompt(_req()).lower()
        assert "photography" not in prompt
        assert "photo" not in prompt

    def test_keywords_are_used_when_there_is_no_prompt(self):
        p = AIImageProvider(FakeBackend([]))
        prompt = p.build_prompt(_req(prompt="", keywords=["monsoon", "field"]))
        assert prompt.startswith("monsoon, field")

    def test_kids_constraints_are_added_only_for_kids(self):
        p = AIImageProvider(FakeBackend([]))
        assert "frightening" in p.build_prompt(_req(made_for_kids=True))
        assert "frightening" not in p.build_prompt(_req(made_for_kids=False))

    def test_a_style_is_always_present(self):
        """An unstyled AI image looks worse than real stock."""
        p = AIImageProvider(FakeBackend([]))
        assert "illustration" in p.build_prompt(_req(style="")).lower()


class TestSeeds:
    def test_the_seed_is_stable_for_a_scene(self):
        p = AIImageProvider(FakeBackend([]))
        assert p._seed_for(_req(3), 0) == p._seed_for(_req(3), 0)

    def test_scenes_get_different_seeds(self):
        p = AIImageProvider(FakeBackend([]))
        seeds = {p._seed_for(_req(i), 0) for i in range(20)}
        assert len(seeds) == 20

    def test_a_retry_moves_the_seed(self):
        p = AIImageProvider(FakeBackend([]))
        assert p._seed_for(_req(1), 0) != p._seed_for(_req(1), 1)


class TestDuplicateDetection:
    """Two identical shots in a row look like a broken render."""

    def test_a_repeated_image_is_regenerated(self, tmp_path):
        same = _png_bytes((20, 90, 40))
        different = _png_bytes((20, 90, 40), noise=200)
        backend = FakeBackend([same, same, different])
        p = AIImageProvider(backend)

        p.fetch(_req(0), tmp_path / "a.jpg")
        p.fetch(_req(1), tmp_path / "b.jpg")

        # Scene 1 got the duplicate first, so it asked again...
        assert len(backend.calls) == 3
        # ...with a different seed.
        assert backend.calls[1][1] != backend.calls[2][1]

    def test_distinct_images_are_not_regenerated(self, tmp_path):
        backend = FakeBackend([_png_bytes((10, 10, 10)),
                               _png_bytes((240, 240, 240))])
        p = AIImageProvider(backend)
        p.fetch(_req(0), tmp_path / "a.jpg")
        p.fetch(_req(1), tmp_path / "b.jpg")
        assert len(backend.calls) == 2

    def test_a_duplicate_is_accepted_rather_than_failing_the_scene(self, tmp_path):
        """A repeated picture is bad. No picture is worse."""
        same = _png_bytes((70, 70, 120))
        backend = FakeBackend([same, same, same, same])
        p = AIImageProvider(backend, max_attempts=2)
        p.fetch(_req(0), tmp_path / "a.jpg")
        asset = p.fetch(_req(1), tmp_path / "b.jpg")
        assert asset.asset == "b.jpg"

    def test_rescuing_a_scene_does_not_flag_it_against_itself(self, tmp_path):
        backend = FakeBackend([_png_bytes((5, 100, 200))])
        p = AIImageProvider(backend)
        p.fetch(_req(4), tmp_path / "a.jpg")
        digest = average_hash(tmp_path / "a.jpg")
        assert p._claim(digest, 4) is None


class TestHash:
    def test_identical_images_hash_identically(self, tmp_path):
        for name in ("a.png", "b.png"):
            (tmp_path / name).write_bytes(_png_bytes((33, 66, 99)))
        assert average_hash(tmp_path / "a.png") == average_hash(tmp_path / "b.png")

    def test_different_images_are_far_apart(self, tmp_path):
        (tmp_path / "a.png").write_bytes(_png_bytes((0, 0, 0), noise=255))
        (tmp_path / "b.png").write_bytes(_png_bytes((255, 255, 255)))
        distance = hamming(average_hash(tmp_path / "a.png"),
                           average_hash(tmp_path / "b.png"))
        assert distance > AIImageProvider.DUPLICATE_DISTANCE


class TestFailureHandling:
    def test_a_transient_error_is_retried(self, tmp_path):
        backend = FakeBackend([RuntimeError("503 loading"),
                               _png_bytes((12, 34, 56))])
        p = AIImageProvider(backend)
        asset = p.fetch(_req(0), tmp_path / "a.jpg")
        assert asset.scene_index == 0
        assert len(backend.calls) == 2

    def test_giving_up_raises_so_the_chain_falls_through(self, tmp_path):
        backend = FakeBackend([RuntimeError("nope")] * 3)
        p = AIImageProvider(backend)
        with pytest.raises(RuntimeError):
            p.fetch(_req(0), tmp_path / "a.jpg")

    def test_a_non_image_body_is_not_accepted(self, tmp_path):
        """Rate-limit pages arrive with a 200 and an HTML body."""
        backend = FakeBackend([b"<html>rate limited</html>"] * 3)
        p = AIImageProvider(backend)
        with pytest.raises(RuntimeError):
            p.fetch(_req(0), tmp_path / "a.jpg")

    def test_the_asset_records_the_backend_not_the_provider(self, tmp_path):
        """The manifest has to say which service made the picture."""
        p = AIImageProvider(FakeBackend([_png_bytes((9, 9, 9))]))
        asset = p.fetch(_req(0), tmp_path / "a.jpg")
        assert asset.source == "generated:fake"
        assert "no third-party rights" in asset.license


class TestBackendSelection:
    def test_the_default_is_keyless(self):
        assert build_backend(load_config()).id == "pollinations"

    def test_huggingface_without_a_token_degrades_instead_of_failing(
            self, monkeypatch):
        cfg = load_config()
        monkeypatch.delenv("HF_API_TOKEN", raising=False)
        cfg.set("visuals.ai_image_backend", "huggingface")
        assert build_backend(cfg).id == "pollinations"

    def test_huggingface_is_used_when_a_token_exists(self, monkeypatch):
        cfg = load_config()
        cfg.set("visuals.ai_image_backend", "huggingface")
        monkeypatch.setenv("HF_API_TOKEN", "hf_dummy_token_value")
        backend = build_backend(cfg)
        assert backend.id == "huggingface"
        assert backend.available()

    def test_the_keyless_backend_needs_no_credentials(self):
        assert PollinationsBackend().available() is True

    def test_the_hf_backend_does_not_use_the_dead_endpoint(self):
        """api-inference.huggingface.co no longer resolves in DNS.

        The first version of the backend posted to it, so it could never have
        worked. Serving moved to Inference Providers behind
        router.huggingface.co, which huggingface_hub routes for us.
        """
        import inspect
        # The docstring explains the dead endpoint, so compare against CODE.
        src = inspect.getsource(HuggingFaceBackend)
        code = src.replace(HuggingFaceBackend.__doc__ or "", "")
        assert "api-inference.huggingface.co" not in code
        assert "InferenceClient" in code

    def test_credit_exhaustion_is_its_own_error(self):
        """A 402 must not be retried three times per scene."""
        from engine.visuals.ai_image import CreditExhausted
        assert issubclass(CreditExhausted, RuntimeError)

    def test_a_402_degrades_to_keyless_instead_of_killing_the_job(self, tmp_path):
        """Otherwise one 402 turns the rest of an illustrated video to shapes."""
        from engine.visuals.ai_image import CreditExhausted
        paid = FakeBackend([CreditExhausted("402")])
        free = FakeBackend([_png_bytes((4, 8, 16))])
        free.id = "keyless"
        p = AIImageProvider(paid, retry_backoff=0, fallback=free)

        asset = p.fetch(_req(0), tmp_path / "a.jpg")

        # The scene still produced a drawn image, from the free backend.
        assert p.backend is free
        assert asset.source == "generated:keyless"
        assert len(free.calls) == 1


# ==========================================================================
class TestCharacterBible:
    def _bible(self):
        return CharacterBible([
            Character("RAJU", "a 10-year-old boy, green shirt"),
            Character("MEENA", "a 9-year-old girl, two braids"),
            Character("GOLU", "a chubby boy, orange shirt"),
        ])

    def test_only_the_characters_in_the_scene_are_described(self):
        clause = self._bible().clause_for("Raju climbed the mango tree")
        assert "RAJU" in clause
        assert "MEENA" not in clause

    def test_a_scene_with_no_names_gets_the_cast(self):
        """Narration says 'the children' while still needing them right."""
        clause = self._bible().clause_for("the children laughed together")
        assert "RAJU" in clause and "MEENA" in clause

    def test_the_clause_is_capped_so_it_cannot_swamp_the_scene(self):
        big = CharacterBible([Character(f"N{i}", "a person") for i in range(6)])
        assert big.clause_for("nobody named here").count(" is ") <= 3

    def test_an_empty_bible_contributes_nothing(self):
        assert CharacterBible().clause_for("Raju ran") == ""
        assert not CharacterBible()

    def test_a_partial_name_does_not_match(self):
        """RAJ must not be found inside RAJU."""
        bible = CharacterBible([Character("RAJ", "a man"),
                                Character("MEENA", "a girl")])
        clause = bible.clause_for("Meena ran home")
        assert "MEENA is" in clause
        assert "RAJ is" not in clause

    def test_descriptions_are_truncated(self):
        bible = CharacterBible.from_dict(
            {"characters": [{"name": "A", "description": "x" * 500}]})
        assert len(bible.characters[0].description) <= 140

    def test_incomplete_entries_are_dropped(self):
        bible = CharacterBible.from_dict({"characters": [
            {"name": "", "description": "a boy"},
            {"name": "B", "description": ""},
            {"name": "C", "description": "a girl"},
        ]})
        assert [c.name for c in bible.characters] == ["C"]

    def test_the_cast_is_capped(self):
        bible = CharacterBible.from_dict({"characters": [
            {"name": f"N{i}", "description": "a person"} for i in range(20)]})
        assert len(bible.characters) <= 6

    def test_it_round_trips_to_json(self, tmp_path):
        import json
        self._bible().save(tmp_path / "cb.json")
        again = CharacterBible.from_dict(
            json.loads((tmp_path / "cb.json").read_text(encoding="utf-8")))
        assert [c.name for c in again.characters] == ["RAJU", "MEENA", "GOLU"]


class TestBuildBible:
    class Router:
        def __init__(self, payload):
            self.payload = payload
            self.prompt = ""

        def complete_json(self, prompt, **kw):
            self.prompt = prompt
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload, "fake"

    def test_a_model_failure_yields_an_empty_bible(self):
        """A wrong bible is worse than none - it repeats into every frame."""
        assert not build_bible("some narration", self.Router(RuntimeError("down")))

    def test_no_router_yields_an_empty_bible(self):
        assert not build_bible("some narration", None)

    def test_empty_narration_makes_no_call(self):
        router = self.Router({"characters": [{"name": "X", "description": "y"}]})
        assert not build_bible("   ", router)
        assert router.prompt == ""

    def test_a_valid_reply_becomes_the_cast(self):
        router = self.Router({"characters": [
            {"name": "RAJU", "description": "a boy in a green shirt"}]})
        bible = build_bible("Raju walked", router)
        assert [c.name for c in bible.characters] == ["RAJU"]

    def test_the_prompt_forbids_inventing_people(self):
        router = self.Router({"characters": []})
        build_bible("The moon orbits the earth.", router)
        assert "Do not invent people" in router.prompt


# ==========================================================================
class TestTemplateRequestsIllustration:
    """A template's medium and its style string have to agree."""

    def test_storytelling_asks_for_generated_images(self):
        from engine.video.templates import TEMPLATES
        assert TEMPLATES["STORYTELLING"].prefer_ai is True

    def test_its_style_describes_a_drawing_not_a_photograph(self):
        """Asking for illustration while serving photos changes medium mid-video."""
        from engine.video.templates import TEMPLATES
        suffix = TEMPLATES["STORYTELLING"].visual_style_suffix.lower()
        assert "illustrat" in suffix
        assert "photograph" not in suffix

    def test_every_ai_template_describes_a_drawn_style(self):
        from engine.video.templates import TEMPLATES
        for name, t in TEMPLATES.items():
            if not t.prefer_ai:
                continue
            suffix = t.visual_style_suffix.lower()
            assert suffix, name
            assert "photograph" not in suffix, name

    def test_pacing_matches_illustrated_storytelling(self):
        """Measured on the reference channel: one image every 6.7 seconds."""
        from engine.video.templates import TEMPLATES
        assert TEMPLATES["STORYTELLING"].scene_seconds >= 5.5

    def test_overrides_are_empty_when_a_template_does_not_care(self):
        from engine.video.templates import TEMPLATES, visual_overrides
        assert visual_overrides(TEMPLATES["FAST_FACTS"]) == {}

    def test_the_override_actually_reorders_the_provider_chain(self):
        from engine.core.config import load_config
        from engine.video.templates import TEMPLATES, visual_overrides
        from engine.visuals.engine import VisualEngine
        cfg = load_config()
        for key, value in visual_overrides(TEMPLATES["STORYTELLING"]).items():
            cfg.set(key, value)
        assert VisualEngine(cfg).providers[0].name == "ai_image"

    def test_the_pipeline_rebuilds_the_visual_engine_after_the_template(self):
        """The chain is decided at construction, so a stale engine ignores it."""
        import inspect
        from engine import pipeline as mod
        src = inspect.getsource(mod.Pipeline)
        block = src[src.index("visual_overrides(template)"):]
        assert "VisualEngine(self.cfg)" in block[:800]


class TestIllustratedChainExcludesStock:
    """A photograph inside an illustrated story is the defect, not the rescue."""

    def _chain(self, prefer_ai: bool):
        from engine.core.config import load_config
        from engine.visuals.engine import VisualEngine
        cfg = load_config()
        cfg.set("visuals.prefer_ai", prefer_ai)
        return [p.name for p in VisualEngine(cfg).providers]

    def test_stock_is_removed_not_demoted(self):
        chain = self._chain(True)
        assert "pexels" not in chain
        assert "pexels_video" not in chain
        assert "pixabay" not in chain

    def test_generation_leads(self):
        assert self._chain(True)[0] == "ai_image"

    def test_procedural_remains_as_a_last_resort(self):
        """A scene must never come back empty."""
        assert self._chain(True)[-1] == "procedural"

    def test_factual_content_still_prefers_real_photographs(self):
        chain = self._chain(False)
        assert chain[0] == "pexels_video"
        assert "ai_image" in chain

    def test_the_ai_path_runs_one_request_at_a_time(self):
        """Two simultaneous requests get one image and one refusal."""
        import inspect
        from engine.visuals import engine as mod
        src = inspect.getsource(mod.VisualEngine.generate)
        assert "visuals.ai_parallel" in src
        assert 'self.providers[0].name == "ai_image"' in src

    def test_a_retry_waits_before_asking_again(self):
        import inspect
        from engine.visuals.ai_image import AIImageProvider
        src = inspect.getsource(AIImageProvider.fetch)
        assert "time.sleep(self.retry_backoff" in src

    def test_the_failure_message_names_the_cause(self):
        """Rate limit, cold model and bad prompt need different responses."""
        from engine.visuals.ai_image import AIImageProvider
        import pytest as _pytest
        backend = FakeBackend([RuntimeError("429 too many requests")] * 3)
        p = AIImageProvider(backend, max_attempts=1, retry_backoff=0)
        with _pytest.raises(RuntimeError, match="429"):
            p.fetch(_req(0), __import__("pathlib").Path("/tmp/none.jpg"))
