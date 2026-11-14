"""The job scripts of the bulk and single-cell pages, calling the lab's Pipelines-Wrappers.

bulk_script(): RNAseq.sh (one run over all samples: STAR, featureCounts, DESeq2 for the chosen comparisons, -c),
ChIPseq.sh (each sample, then MACS2 callpeak with its input / IgG control, or none), ATACseq.sh (each sample, MACS2
options with -M). The samples' FASTQ files are linked (one lane) or concatenated (several lanes) into
<output>/fastq/<name>_R1.fastq.gz, so the page's sample names become the pipelines' prefixes.
singlecell_script(): cellranger.sh count for each sample (GEX, ATAC, or multiome with a libraries CSV), optionally
aggr, then optionally scripts/sc_analysis.R (Seurat / Signac: QC, filtering, clustering, label transfer, RDS /
h5ad / h5mu).
check_paths(): what must exist on the server that runs the job (wrappers, references, tools, inputs) and that the
output folder can be written. Every value from the page reaches the script through shlex.quote.
"""

import re
import shlex
from itertools import combinations
from pathlib import Path

from fastq import safe_name

APP_DIR = Path(__file__).resolve().parent
SC_SCRIPT = APP_DIR / "scripts" / "sc_analysis.R"
q = shlex.quote

GENOMES_BULK = {"hg38": ("hg", "hs"), "hg19": ("hg", "hs"), "mm10": ("mm", "mm")}
MACS2_ATAC_DEFAULT = "--keep-dup all --nomodel --shift -75 --extsize 150 -q 0.05"
HELPERS = r"""
wb_step() { echo "##WB step $*"; }
wb_progress() { echo "##WB progress $*"; }
# one file: a link; several lanes: concatenated (gzip members join into one valid .gz)
wb_fastq() { local out=$1; shift; if [ $# -eq 1 ]; then ln -sfn "$1" "$out"; else cat "$@" > "$out.part" && mv "$out.part" "$out"; fi; }
"""


def wrapper_defaults(wrappers, script):
    """name -> value of the simple assignments at the top of a wrapper (index and reference paths)."""
    values = {}
    try:
        text = (Path(wrappers) / script).read_text(errors="replace")
    except OSError:
        return values
    for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)=['\"]?([^'\"\n$`]+)['\"]?\s*$", text, re.M):
        values[m.group(1)] = m.group(2).strip()
    return values


def name_ok(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name or ""):
        raise ValueError("Sample and group names may use letters, digits, _ . - (got {!r})".format(name))
    return name


def fastq_links(samples, paired):
    lines = ["mkdir -p fastq", "wb_step 'Linking FASTQ files into fastq/'"]
    for s in samples:
        n = name_ok(s["name"])
        lines.append("wb_fastq {} {}".format(q("fastq/{}_R1.fastq.gz".format(n) if paired else "fastq/{}.fastq.gz".format(n)),
                                             " ".join(q(p) for p in s["r1"])))
        if paired:
            if len(s.get("r2") or []) != len(s["r1"]):
                raise ValueError("{}: R1 and R2 files do not pair up".format(n))
            lines.append("wb_fastq {} {}".format(q("fastq/{}_R2.fastq.gz".format(n)), " ".join(q(p) for p in s["r2"])))
    return lines


def reads(name, paired):
    return ["fastq/{}_R1.fastq.gz".format(name), "fastq/{}_R2.fastq.gz".format(name)] if paired else ["fastq/{}.fastq.gz".format(name)]


def bulk_script(p, wrappers):
    """p: {assay: rnaseq|chipseq|cutrun|cuttag|atacseq, output, samples: [{name, r1, r2, group, control, include}],
    genome, threads, paired, nextera, aligner (bowtie2|bwa|chromap), mapq, comparisons, peaks: {...}, macs2}"""
    assay = p["assay"]
    samples = [s for s in p["samples"] if s.get("include", True)]
    if not samples:
        raise ValueError("Choose at least one sample")
    names = [name_ok(s["name"]) for s in samples]
    if len(set(names)) != len(names):
        raise ValueError("Sample names must be unique")
    threads = int(p.get("threads") or 8)
    paired = bool(p.get("paired", True))
    genome = p.get("genome") or "hg38"
    lines = ["# Omics Workbench: {} with Pipelines-Wrappers ({})".format(assay, wrappers), HELPERS,
             "WRAP={}".format(q(wrappers)), "mkdir -p {0} && cd {0}".format(q(p["output"]))]
    lines += fastq_links(samples, paired)
    keep_going = bool(p.get("continue_on_error"))
    if assay == "rnaseq":
        if not paired:
            raise ValueError("RNAseq.sh needs paired-end reads")
        groups = {s["name"]: name_ok(s.get("group") or "") for s in samples}
        if len(set(groups.values())) < 2:
            raise ValueError("DESeq2 needs at least two groups")
        lines.append("declare -A GROUP=({})".format(" ".join("[{}]={}".format(q(n), q(g)) for n, g in groups.items())))
        lines += ["printf 'Sample\\tGroup\\n' > meta.txt",
                  "# the order RNAseq.sh reads the files in (the same glob), so meta.txt matches featureCounts' columns",
                  "for f in fastq/*_R1.fastq.gz; do n=${f##*/}; n=${n%_R1.fastq.gz}; printf '%s\\t%s\\n' \"$n\" \"${GROUP[$n]}\" >> meta.txt; done",
                  "cat meta.txt"]
        args = ["-m", "meta.txt", "-t", str(threads)]
        if p.get("star_index") or p.get("gtf"):
            if not (p.get("star_index") and p.get("gtf")):
                raise ValueError("Give both the STAR index and the GTF, or neither")
            args += ["-i", p["star_index"], "-g", p["gtf"]]
        else:
            args += ["-s", GENOMES_BULK.get(genome, ("hg",))[0] if genome in GENOMES_BULK else "hg"]
        if p.get("nextera"):
            args.append("-n")
        comparisons = p.get("comparisons") or []
        valid = set(groups.values())
        for a, b in comparisons:
            if a not in valid or b not in valid or a == b:
                raise ValueError("Comparison {} vs {}: both must be different groups of the samples".format(a, b))
        if comparisons:
            lines.append("printf '%s\\t%s\\n' {} > comparisons.txt".format(" ".join(q(x) for pair in comparisons for x in pair)))
            args += ["-c", "comparisons.txt"]
        lines += ["wb_step 'RNAseq.sh: cutadapt, STAR, featureCounts, DESeq2 ({} samples)'".format(len(samples)),
                  "bash \"$WRAP/RNAseq.sh\" {} fastq/".format(" ".join(q(a) for a in args))]
        return "\n".join(lines) + "\n"

    # ChIP-seq family and ATAC-seq: one wrapper run per sample
    script = "ATACseq.sh" if assay == "atacseq" else "ChIPseq.sh"
    common = ["-g", genome, "-t", str(threads)]
    if not paired:
        common.append("-s")
    if assay == "cutrun":
        common.append("-R")
    elif assay == "cuttag":
        common.append("-T")
    aligner = p.get("aligner") or ""
    if aligner == "chromap":
        common.append("-c")
    if assay in ("chipseq",) and aligner == "bwa-aln":
        common.append("-a")
    if assay != "atacseq":
        if p.get("mapq") not in (None, ""):
            common += ["-q", str(int(p["mapq"]))]
        if p.get("nextera"):
            common.append("-n")
    if assay == "atacseq":
        common += ["-M", (p.get("macs2") or MACS2_ATAC_DEFAULT).strip()]
    lines += ["failed=()", "total={}".format(len(samples)), "i=0"]
    for s in samples:
        n = s["name"]
        cmd = "bash \"$WRAP/{}\" {} -p {} {}".format(script, " ".join(q(a) for a in common), q(n), " ".join(q(r) for r in reads(n, paired)))
        lines += ["i=$((i + 1)); wb_progress $(( (i - 1) * 100 / total ))",
                  "wb_step \"{} {} ($i of $total)\"".format(script, n)]
        if keep_going:
            lines.append("if ! ( {} ); then echo \"##WB sample failed: {}\"; failed+=({}); fi".format(cmd, n, q(n)))
        else:
            lines.append(cmd)
    peaks = p.get("peaks") or {}
    if assay != "atacseq" and peaks.get("call"):
        species = GENOMES_BULK.get(genome, (None, "hs"))[1] if genome in GENOMES_BULK else (peaks.get("gsize") or "hs")
        controls = {s["name"]: s.get("control") or "" for s in samples}
        treated = [s["name"] for s in samples if not s.get("is_control")]
        lines += ["mkdir -p macs2", "wb_step 'MACS2 callpeak'"]
        for n in treated:
            c = controls.get(n) or ""
            if c and c not in names:
                raise ValueError("Control {} of {} is not among the samples".format(c, n))
            if aligner == "chromap":
                t_file, c_file, fmt = "{}_pri.bed".format(n), "{}_pri.bed".format(c), "BEDPE" if paired else "BED"
            else:
                t_file, c_file, fmt = "{}_filtered.bam".format(n), "{}_filtered.bam".format(c), "BAMPE" if paired else "BAM"
            args = ["-t", t_file] + (["-c", c_file] if c else []) + ["-f", fmt, "-g", species, "-n", n, "--outdir", "macs2"]
            if peaks.get("broad"):
                args += ["--broad", "--broad-cutoff", str(float(peaks.get("broad_cutoff") or 0.1))]
            if peaks.get("pvalue"):
                args += ["-p", str(float(peaks["pvalue"]))]
            else:
                args += ["-q", str(float(peaks.get("qvalue") or 0.05))]
            extra = shlex.split(peaks.get("extra") or "")
            cmd = "macs2 callpeak {}".format(" ".join(q(a) for a in args + extra))
            lines.append("if [ -e {} ]; then {}; else echo 'MACS2: {} has no alignments ({} missing)'; failed+=({}); fi".format(
                q(t_file), cmd, n, t_file, q(n + "_peaks")))
    lines += ["wb_progress 100",
              "if [ ${#failed[@]} -gt 0 ]; then echo \"##WB step Finished with failures: ${failed[*]}\"; exit 1; fi",
              "wb_step 'All samples finished'"]
    return "\n".join(lines) + "\n"


def comparisons_all(groups):
    order = []
    for g in groups:
        if g not in order:
            order.append(g)
    return [list(pair) for pair in combinations(order, 2)]


# ---------------------------------------------------------------------- single cell

SC_MODES = {"gex": "rna", "atac": "atac", "multiome": "multiome"}


def singlecell_script(p, wrappers):
    """p: {mode: gex|atac|multiome, output, samples: [{name, prefix, dirs}] (gex/atac) or pairs: [{name, gex:
    {prefix, dirs}, atac: {prefix, dirs}}] (multiome), genome, reference, threads, memory, rna_version, create_bam,
    secondary, aggr, seurat: {run, review, nmad, reference, ref_label, integrate, resolution, formats, ...}}"""
    mode = p["mode"]
    if mode not in SC_MODES:
        raise ValueError("Unknown single-cell mode")
    threads, memory = int(p.get("threads") or 16), int(p.get("memory") or 128)
    common = ["-m", SC_MODES[mode], "-t", str(threads), "-r", str(memory)]
    if p.get("reference"):
        common += ["-x", p["reference"]]
    else:
        common += ["-g", p.get("genome") or "hg38"]
    if mode == "gex":
        common += ["-u", str(int(p.get("rna_version") or 10))]
    if mode in ("gex", "multiome"):
        common += ["--create-bam", "true" if p.get("create_bam", True) else "false"]
    if p.get("secondary") is False:
        common.append("-S")
    lines = ["# Omics Workbench: Cell Ranger ({}) with Pipelines-Wrappers ({})".format(mode, wrappers), HELPERS,
             "WRAP={}".format(q(wrappers)), "mkdir -p {0} && cd {0}".format(q(p["output"]))]
    runs = []
    if p.get("skip_count"):
        runs = [{"name": name_ok(s["name"]), "outs": s["outs"]} for s in p.get("existing") or []]
        if not runs:
            raise ValueError("Choose the Cell Ranger outputs to analyse")
    else:
        items = p.get("pairs") if mode == "multiome" else p.get("samples")
        items = [s for s in items or [] if s.get("include", True)]
        if not items:
            raise ValueError("Choose at least one sample")
        names = [name_ok(s["name"]) for s in items]
        if len(set(names)) != len(names):
            raise ValueError("Run names must be unique")
        lines += ["total={}".format(len(items)), "i=0"]
        for s in items:
            n = s["name"]
            done = "[ -s {} ]".format(q("{}/outs/web_summary.html".format(n)))
            lines += ["i=$((i + 1)); wb_progress $(( (i - 1) * 100 / total ))"]
            if mode == "multiome":
                if not s.get("gex") or not s.get("atac"):
                    raise ValueError("{}: choose both its GEX and its ATAC library".format(n))
                rows = ["fastqs,sample,library_type"]
                for lib, kind in ((s["gex"], "Gene Expression"), (s["atac"], "Chromatin Accessibility")):
                    for d in lib["dirs"]:
                        if "," in d or "," in lib["prefix"]:
                            raise ValueError("Folder and sample names cannot contain commas: {}".format(d))
                        rows.append("{},{},{}".format(d, lib["prefix"], kind))
                csv = "libraries_{}.csv".format(n)
                lines.append("printf '%s\\n' {} > {}".format(" ".join(q(r) for r in rows), q(csv)))
                cmd = "bash \"$WRAP/cellranger.sh\" {} --id {} --libraries {}".format(" ".join(q(a) for a in common), q(n), q(csv))
            else:
                cmd = "bash \"$WRAP/cellranger.sh\" {} --id {} --sample {} {}".format(
                    " ".join(q(a) for a in common), q(n), q(s["prefix"]), q(",".join(s["dirs"])))
            lines += ["if {}; then echo 'Cell Ranger: {} is already complete, skipped'; else".format(done, n),
                      "  wb_step \"Cell Ranger {} ($i of $total)\"".format(n),
                      "  " + cmd, "fi"]
            runs.append({"name": n, "outs": "{}/{}/outs".format(p["output"].rstrip("/"), n)})
        if p.get("aggr") and len(items) > 1:
            agg = "aggr_" + safe_name(p.get("aggr_name") or "all")
            rows = {"gex": "sample_id,molecule_h5", "atac": "library_id,fragments,cells",
                    "multiome": "library_id,atac_fragments,per_barcode_metrics,gex_molecule_info"}[mode]
            files = {"gex": ["molecule_info.h5"], "atac": ["fragments.tsv.gz", "singlecell.csv"],
                     "multiome": ["atac_fragments.tsv.gz", "per_barcode_metrics.csv", "gex_molecule_info.h5"]}[mode]
            csv_rows = [rows] + [",".join([r["name"]] + ["{}/{}".format(r["outs"], f) for f in files]) for r in runs]
            aggr_args = ["-m", SC_MODES[mode], "-a", "--id", agg, "--csv", "aggr_{}.csv".format(agg[5:]), "-t", str(threads), "-r", str(memory),
                         "--normalize", p.get("aggr_normalize") or "none"]
            if mode != "gex":
                aggr_args += ["-x", p["reference"]] if p.get("reference") else ["-g", p.get("genome") or "hg38"]
            lines += ["printf '%s\\n' {} > {}".format(" ".join(q(r) for r in csv_rows), q("aggr_{}.csv".format(agg[5:]))),
                      "wb_step 'Cell Ranger aggr'",
                      "if [ -s {} ]; then echo 'aggr already complete'; else bash \"$WRAP/cellranger.sh\" {}; fi".format(
                          q("{}/outs/web_summary.html".format(agg)), " ".join(q(a) for a in aggr_args))]
    seurat = p.get("seurat") or {}
    if seurat.get("run"):
        lines += seurat_lines(p, runs, seurat)
    lines += ["wb_progress 100", "wb_step 'Finished'"]
    return "\n".join(lines) + "\n"


def seurat_lines(p, runs, s):
    mode = p["mode"]
    out = s.get("output") or "{}/seurat".format(p["output"].rstrip("/"))
    stage = s.get("stage") or ("qc" if s.get("review") else "full")
    formats = s.get("formats") or ["rds"]
    allowed = {"gex": {"rds", "h5ad"}, "atac": {"rds", "h5ad"}, "multiome": {"rds", "h5mu"}}[mode]
    formats = [f for f in formats if f in allowed] or ["rds"]
    args = ["--mode", mode, "--stage", stage, "--samples", "{}/samples.tsv".format(out), "--out", out,
            "--formats", ",".join(formats), "--threads", str(int(p.get("threads") or 8)),
            "--nmad", str(float(s.get("nmad") or 3)), "--integrate", "harmony" if s.get("integrate", True) else "none",
            "--resolution", re.sub(r"[^0-9.,]", "", str(s.get("resolution") or "0.4,0.8")) or "0.4",
            "--name", safe_name(s.get("name") or Path(p["output"]).name or "analysis")]
    genome = p.get("genome") or ""
    if genome:
        args += ["--genome", genome]
    if s.get("thresholds_file"):
        args += ["--thresholds", s["thresholds_file"]]
    if s.get("ref"):
        args += ["--reference", s["ref"], "--ref-label", s.get("ref_label") or "auto",
                 "--ref-min-score", str(float(s.get("ref_min_score") or 0.5))]
    if s.get("doublets"):
        args += ["--doublets", "scDblFinder"]
    rows = ["sample_id\tpath"] + ["{}\t{}".format(r["name"], r["outs"]) for r in runs]
    return ["mkdir -p {}".format(q(out)),
            "printf '%s\\n' {} > {}".format(" ".join(q(r) for r in rows), q("{}/samples.tsv".format(out))),
            "wb_step 'Seurat analysis ({})'".format("QC and proposed thresholds" if stage == "qc" else "full"),
            "Rscript {} {}".format(q(str(SC_SCRIPT)), " ".join(q(a) for a in args))]


# ---------------------------------------------------------------------- checks

BULK_TOOLS = {"rnaseq": ["cutadapt", "STAR", "featureCounts", "Rscript", "fastqc", "python3"],
              "chipseq": ["cutadapt", "samtools", "bedtools", "bamCoverage", "bwa", "java", "macs2"],
              "cutrun": ["cutadapt", "samtools", "bedtools", "bamCoverage", "bowtie2", "java", "macs2"],
              "cuttag": ["cutadapt", "samtools", "bedtools", "bamCoverage", "bowtie2", "java", "macs2"],
              "atacseq": ["cutadapt", "samtools", "bedtools", "bamCoverage", "bowtie2", "java", "macs2", "fastqc"]}
SC_TOOLS = {"gex": ["cellranger"], "atac": ["cellranger-atac"], "multiome": ["cellranger-arc"]}


def bulk_requirements(p, wrappers):
    assay, genome = p["assay"], p.get("genome") or "hg38"
    script = {"rnaseq": "RNAseq.sh", "atacseq": "ATACseq.sh"}.get(assay, "ChIPseq.sh")
    need = [("wrapper", str(Path(wrappers) / script))]
    v = wrapper_defaults(wrappers, script)
    short = {"hg38": "hg38", "hg19": "hg19", "mm10": "mm10"}.get(genome, genome)
    if assay == "rnaseq":
        if p.get("star_index"):
            need += [("STAR index", p["star_index"]), ("GTF", p["gtf"])]
        else:
            sp = GENOMES_BULK.get(genome, ("hg",))[0]
            need += [("STAR index", v.get("STAR_idx_" + sp, "")), ("GTF", v.get("gtf_" + sp, ""))]
    elif p.get("aligner") == "chromap":
        need += [("chromap index", v.get("chromapindex_" + short, "")), ("genome FASTA", v.get("chromapref_" + short, ""))]
    elif assay in ("cutrun", "cuttag", "atacseq"):
        need += [("Bowtie2 index", v.get("bw2index_" + short, "") + ".1.bt2")]
    else:
        need += [("BWA index", v.get("bwaindex_" + short, "") + ".bwt")]
    if assay != "rnaseq":
        need += [("chrom sizes", v.get("chromszize_" + short, ""))]
        if p.get("paired", True) and p.get("aligner") != "chromap":
            need += [("Picard", v.get("picard_path", ""))]
    tools = list(BULK_TOOLS[assay])
    if p.get("aligner") == "chromap":
        tools = [t for t in tools if t not in ("bwa", "bowtie2", "java")] + ["chromap", "bedGraphToBigWig", "bedSort"]
    return [(k, x) for k, x in need if x], tools


def singlecell_requirements(p, wrappers):
    need = [("wrapper", str(Path(wrappers) / "cellranger.sh"))]
    v = wrapper_defaults(wrappers, "cellranger.sh")
    mode, genome = p["mode"], (p.get("genome") or "hg38").upper()
    if p.get("reference"):
        need.append(("reference", p["reference"]))
    elif not p.get("skip_count"):
        key = "{}_{}_DEFAULT".format("ARC" if mode == "multiome" else "RNA", genome)
        if mode == "atac":
            key = "ARC_{}_DEFAULT".format(genome)  # cellranger.sh uses the ARC reference for ATAC
        if v.get(key):
            need.append(("reference ({})".format(genome.lower()), v[key]))
    tools = [] if p.get("skip_count") else list(SC_TOOLS[mode])
    if p.get("mode") == "gex" and str(p.get("rna_version") or 10) != "10":
        tools = ["cellranger{}".format(int(p["rna_version"]))]
    s = p.get("seurat") or {}
    if s.get("run"):
        need.append(("analysis script", str(SC_SCRIPT)))
        if s.get("ref"):
            need.append(("label reference", s["ref"]))
        tools.append("Rscript")
    return need, tools


def check_paths(target, need, tools, inputs, output):
    """Run the checks on the target: [{what, path, ok, detail}] and the tools found."""
    lines = []
    for i, (what, path) in enumerate(need):
        lines.append("if [ -e {0} ]; then echo 'need\t{1}\tok'; else echo 'need\t{1}\tmissing'; fi".format(q(path), i))
    for i, path in enumerate(inputs):
        lines.append("if [ -r {0} ]; then echo 'input\t{1}\tok'; elif [ -e {0} ]; then echo 'input\t{1}\tunreadable'; "
                     "else echo 'input\t{1}\tmissing'; fi".format(q(path), i))
    if output:
        lines.append("if mkdir -p {0} 2>/dev/null && [ -w {0} ] && t=$(mktemp -p {0} .wbcheck.XXXXXX 2>/dev/null); then rm -f \"$t\"; "
                     "echo \"output\t0\tok\t$(df -Ph {0} | awk 'NR==2{{print $4}}')\"; else echo 'output\t0\tnot writable'; fi".format(q(output)))
    if tools:
        lines.append("timeout 60 bash -ic {} 2>/dev/null </dev/null".format(q(
            "for t in {}; do p=$(command -v \"$t\"); echo \"tool\t$t\t${{p:-missing}}\"; done".format(" ".join(q(t) for t in tools)))))
    code, out, err = target.run("\n".join(lines), timeout=120)
    results = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        kind = parts[0]
        if kind == "need":
            what, path = need[int(parts[1])]
            results.append({"what": what, "path": path, "ok": parts[2] == "ok", "detail": parts[2]})
        elif kind == "input":
            results.append({"what": "input", "path": inputs[int(parts[1])], "ok": parts[2] == "ok", "detail": parts[2]})
        elif kind == "output":
            results.append({"what": "output folder", "path": output, "ok": parts[2] == "ok",
                            "detail": "writable, {} free".format(parts[3]) if parts[2] == "ok" and len(parts) > 3 else parts[2]})
        elif kind == "tool":
            results.append({"what": "program", "path": parts[1], "ok": parts[2] != "missing", "detail": parts[2]})
    return results
