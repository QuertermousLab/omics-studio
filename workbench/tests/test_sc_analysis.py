"""Checks of workbench/scripts/sc_analysis.R that need no data: the threshold rule (rows of the approved Amgen
threshold_derivation.tsv), argument parsing and input validation. Skipped where Rscript is missing.

  python -m unittest discover workbench/tests
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sc_analysis.R"


def run(*args):
    return subprocess.run(["Rscript", str(SCRIPT), *args], capture_output=True, text=True, timeout=300)


@unittest.skipUnless(shutil.which("Rscript"), "Rscript not installed")
class ScAnalysisTest(unittest.TestCase):
    def test_self_test(self):
        r = run("--self-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("self-test OK", r.stdout)

    def test_bad_options_fail_clearly(self):
        r = run("--mode", "rna")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--mode must be gex, atac or multiome", r.stderr)
        self.assertIn("##WB step Failed", r.stdout)
        r = run("--mode", "gex", "--bogus", "1")
        self.assertIn("Unknown option --bogus", r.stderr)

    def test_input_validation(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "s1" / "outs").mkdir(parents=True)
            (d / "s1" / "outs" / "filtered_feature_bc_matrix.h5").write_bytes(b"")
            samples = d / "samples.tsv"
            samples.write_text("sample_id\tpath\ns1\t{}\n".format(d / "s1"))
            # a gene expression folder cannot run as multiome
            r = run("--mode", "multiome", "--stage", "qc", "--samples", str(samples), "--out", str(d / "out"))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("needs cellranger-arc outs", r.stderr)
            # missing folder
            samples.write_text("sample_id\tpath\ns1\t{}\n".format(d / "nope"))
            r = run("--mode", "gex", "--stage", "qc", "--samples", str(samples), "--out", str(d / "out"))
            self.assertIn("folder not found", r.stderr)
            # formats must suit the mode
            r = run("--mode", "gex", "--stage", "full", "--samples", str(samples), "--out", str(d / "out"),
                    "--formats", "h5mu")
            self.assertIn("--formats for --mode gex", r.stderr)


if __name__ == "__main__":
    unittest.main()
