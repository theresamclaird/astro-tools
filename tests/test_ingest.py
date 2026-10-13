"""Tests for tools/ingest.py using a synthetic archive and fake Seestar output.

    python3 -m unittest discover tests
"""
import os, re, shutil, subprocess, sys, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INGEST = os.path.join(REPO, "tools", "ingest.py")


def fake_fits(path, creator, filt="IRCUT", obj="M 31", size=20000):
    cards = ["SIMPLE  =                    T", "BITPIX  =                   16", "NAXIS   =                    0",
             "CREATOR = '%s'" % creator, "FILTER  = '%s'" % filt, "OBJECT  = '%s'" % obj, "END"]
    hdr = "".join(c.ljust(80) for c in cards).ljust(2880).encode("latin1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(hdr + os.urandom(size))


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arc = os.path.join(self.tmp, "Astro")
        for t in ("M31_Andromeda-Galaxy", "NGC6946_Fireworks-Galaxy", "Moon"):
            os.makedirs(os.path.join(self.arc, "Targets", t))
        touch(os.path.join(self.arc, "MANIFEST.sha256"), b"")
        with open(os.path.join(self.arc, "Targets", "M31_Andromeda-Galaxy", "notes.md"), "w") as fh:
            fh.write("# M31\n\n## Raw sessions\n\nold table\n\n## Notes\n\n- keep me\n")
        s50 = os.path.join(self.tmp, "src", "MyWorks")
        self.src = s50
        for d, t in (("20261011", "201500"), ("20261012", "013000")):   # one night, both sides of midnight
            n = "Light_mosaic_M 31_10.0s_IRCUT_%s-%s" % (d, t)
            fake_fits(os.path.join(s50, "M 31_mosaic_sub", n + ".fit"), "ZWO Seestar S50 Pro")
            touch(os.path.join(s50, "M 31_mosaic_sub", n + ".jpg"))
            touch(os.path.join(s50, "M 31_mosaic_sub", n + "_thn.jpg"))
        fake_fits(os.path.join(s50, "C 12_sub", "Light_C 12_10.0s_IRCUT_20261011-210000.fit"), "ZWO Seestar S50 Pro")
        fake_fits(os.path.join(s50, "NGC 7331_sub", "Light_NGC 7331_10.0s_LP_20261011-220000.fit"),
                  "ZWO Seestar S30", filt="LP")
        touch(os.path.join(s50, "M 31_mosaic", "Stacked_2_mosaic_M 31_10.0s_IRCUT_20261012-013000.fit"))
        touch(os.path.join(s50, "M 31_mosaic", "Stacked_2_mosaic_M 31_10.0s_IRCUT_20261012-013000_thn.jpg"))
        touch(os.path.join(s50, "Lunar_video", "2026-10-11-203000-Lunar.mp4"))
        touch(os.path.join(s50, "random.txt"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_ingest(self, *args):
        p = subprocess.run([sys.executable, INGEST, self.src, "--archive", self.arc] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        return p.returncode, p.stdout

    def exists(self, rel):
        return os.path.exists(os.path.join(self.arc, rel))

    def test_dry_run_changes_nothing(self):
        rc, out = self.run_ingest("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("Dry run", out)
        self.assertFalse(self.exists("Targets/M31_Andromeda-Galaxy/raw"))

    def test_ingest_layout_manifest_notes_and_rerun(self):
        rc, out = self.run_ingest("--yes")
        self.assertEqual(rc, 0, out)
        night = "Targets/M31_Andromeda-Galaxy/raw/2026-10-11_S50_mosaic_IRCUT/"
        self.assertTrue(self.exists(night + "Light_mosaic_M 31_10.0s_IRCUT_20261011-201500.fit"))
        self.assertTrue(self.exists(night + "Light_mosaic_M 31_10.0s_IRCUT_20261012-013000.fit"))  # after midnight
        self.assertTrue(self.exists(night + "previews/Light_mosaic_M 31_10.0s_IRCUT_20261012-013000_thn.jpg"))
        self.assertTrue(self.exists("Targets/NGC6946_Fireworks-Galaxy/raw/2026-10-11_S50_IRCUT/"
                                    "Light_C 12_10.0s_IRCUT_20261011-210000.fit"))           # Caldwell alias
        self.assertTrue(self.exists("Targets/NGC7331/raw/2026-10-11_S30_LP/"
                                    "Light_NGC 7331_10.0s_LP_20261011-220000.fit"))          # new target, S30 from header
        self.assertTrue(self.exists("Targets/M31_Andromeda-Galaxy/seestar-stacks/"
                                    "Stacked_2_mosaic_M 31_10.0s_IRCUT_20261012-013000.fit"))
        self.assertFalse(self.exists("Targets/M31_Andromeda-Galaxy/seestar-stacks/"
                                     "Stacked_2_mosaic_M 31_10.0s_IRCUT_20261012-013000_thn.jpg"))
        self.assertTrue(self.exists("Targets/Moon/raw/2026-10-11_Seestar/2026-10-11-203000-Lunar.mp4"))
        self.assertIn("random.txt", out)                                                    # reported, not copied

        # manifest entries verify
        import hashlib
        lines = open(os.path.join(self.arc, "MANIFEST.sha256")).read().splitlines()
        self.assertEqual(len(lines), 10)
        for line in lines:
            digest, rel = line[:64], line[66:]
            with open(os.path.join(self.arc, rel), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, rel)

        # notes.md: table refreshed, user notes kept
        notes = open(os.path.join(self.arc, "Targets/M31_Andromeda-Galaxy/notes.md")).read()
        self.assertIn("`2026-10-11_S50_mosaic_IRCUT` | 2 |", notes)
        self.assertNotIn("old table", notes)
        self.assertIn("- keep me", notes)

        # re-run copies nothing
        rc, out = self.run_ingest("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("Nothing new to ingest", out)

    def test_conflict_is_not_overwritten(self):
        dst = os.path.join(self.arc, "Targets/NGC6946_Fireworks-Galaxy/raw/2026-10-11_S50_IRCUT/"
                                     "Light_C 12_10.0s_IRCUT_20261011-210000.fit")
        touch(dst, b"different")
        rc, out = self.run_ingest("--yes")
        self.assertIn("CONFLICTS", out)
        self.assertEqual(open(dst, "rb").read(), b"different")


if __name__ == "__main__":
    unittest.main()
