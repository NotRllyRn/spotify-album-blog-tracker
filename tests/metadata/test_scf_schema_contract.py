import json
import unittest
from pathlib import Path

from src.album_metadata import schema


EXPORT = Path(__file__).parents[2] / "scf-export-2026-07-24.json"


class SCFSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.objects = json.loads(EXPORT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AssertionError(f"Could not read active SCF contract: {error}") from error

    def test_active_field_group_contract(self):
        groups = [item for item in self.objects if item.get("active") and "fields" in item]
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
        self.assertEqual(
            set(fields),
            set(schema.AUTO_FILLABLE_FIELDS) | set(schema.EDITOR_OWNED_ACF_FIELDS),
        )
        self.assertEqual(set(schema.APPROVED_ACF_TYPES), set(schema.AUTO_FILLABLE_FIELDS))
        self.assertTrue(schema.REMOVED_ACF_FIELDS.isdisjoint(fields))
        for name in schema.EDITOR_OWNED_ACF_FIELDS:
            self.assertIn(name, fields)
            self.assertNotIn(name, schema.AUTO_FILLABLE_FIELDS)
        for name in ("music_release_date", "music_listened_at"):
            self.assertEqual(
                (fields[name].get("display_format"), fields[name].get("return_format")),
                ("d/m/Y", "d/m/Y"),
            )
        tracks = fields["music_tracks"]
        track_fields = {field["name"]: field["type"] for field in tracks["sub_fields"]}
        self.assertEqual(track_fields, {
            "title": "text", "highlight": "true_false", "disc_number": "number",
            "track_number": "number", "duration_ms": "number",
            "explicit": "true_false", "spotify_id": "text",
        })
        self.assertEqual(set(track_fields), set(schema.TRACK_KEYS))

    def test_active_taxonomies_use_default_rest_slugs(self):
        taxonomies = {item["taxonomy"]: item for item in self.objects
                      if item.get("active") and "taxonomy" in item}
        self.assertEqual(set(taxonomies), set(schema.TAXONOMIES))
        for name, taxonomy in taxonomies.items():
            with self.subTest(taxonomy=name):
                self.assertEqual(taxonomy.get("object_type"), ["post"])
                self.assertEqual(taxonomy.get("show_in_rest"), 1)
                self.assertEqual(taxonomy.get("rest_base") or name, name)


if __name__ == "__main__":
    unittest.main()
