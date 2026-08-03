import json
import unittest
from pathlib import Path

import post_to_album as mod


EXPORT = Path(__file__).parents[1] / "scf-export-2026-07-24.json"


class SCFSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objects = json.loads(EXPORT.read_text(encoding="utf-8"))

    def test_active_field_group_contract(self):
        groups = [item for item in self.objects if item.get("active") is True and "fields" in item]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.get("show_in_rest"), 1)
        fields = {field["name"]: field for field in group["fields"]}
        self.assertEqual({name: field["type"] for name, field in fields.items()}, {
            "spotify_title": "text", "music_rating": "number",
            "music_release_date": "date_picker", "music_favorite": "true_false",
            "music_listened_at": "date_picker", "music_notes": "text",
            "music_tracks": "repeater", "music_length_ms": "number",
            "music_avg_track_ms": "number", "music_explicit": "true_false",
            "music_total_tracks": "number", "listen_count": "number",
            "spotify_album_id": "text", "spotify_album_url": "url",
            "lastfm_url": "url", "mbid": "text",
        })
        self.assertNotIn("lastfm_release_id", fields)
        for name in ("music_rating", "music_favorite", "music_notes"):
            self.assertIn(name, fields)
            self.assertNotIn(name, mod.AUTO_FILLABLE_FIELDS)
        for name in ("music_release_date", "music_listened_at"):
            self.assertEqual(
                (fields[name].get("display_format"), fields[name].get("return_format")),
                ("d/m/Y", "d/m/Y"),
            )
        tracks = fields["music_tracks"]
        self.assertEqual({field["name"]: field["type"] for field in tracks["sub_fields"]}, {
            "title": "text", "highlight": "true_false", "disc_number": "number",
            "track_number": "number", "duration_ms": "number",
            "explicit": "true_false", "spotify_id": "text",
        })

    def test_active_taxonomies_use_default_rest_slugs(self):
        taxonomies = {item["taxonomy"]: item for item in self.objects
                      if item.get("active") is True and "taxonomy" in item}
        self.assertEqual(set(taxonomies), {"artist", "genre", "release_type"})
        for name, taxonomy in taxonomies.items():
            with self.subTest(taxonomy=name):
                self.assertEqual(taxonomy.get("object_type"), ["post"])
                self.assertEqual(taxonomy.get("show_in_rest"), 1)
                self.assertEqual(taxonomy.get("rest_base") or name, name)


if __name__ == "__main__":
    unittest.main()
