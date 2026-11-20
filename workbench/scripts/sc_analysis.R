#!/usr/bin/env Rscript
# Omics Workbench single-cell analysis: Cell Ranger outs -> QC -> Seurat / Signac analysis -> RDS / h5ad / h5mu.
#
#   Rscript sc_analysis.R --mode gex|atac|multiome --stage qc|full --samples samples.tsv --out DIR [options]
#   Rscript sc_analysis.R --inspect-reference ref.rds      one JSON line describing the reference's label columns
#   Rscript sc_analysis.R --self-test                      checks of the threshold rule and argument parsing
#   Rscript sc_analysis.R --help
#
# Stage qc computes per-cell QC metrics for every sample and proposes per-sample thresholds (the rule of the
# approved Amgen 2607/2610 multiome QC: log10(value + 1) median +/- nMAD x normalized MAD on cells that pass the
# quality criteria, information floors, caps, outward rounding). Stage full filters with the proposed or the
# user-edited thresholds and runs SCTransform v2 / TF-IDF + LSI, Harmony, WNN, clustering, UMAP, label transfer
# and markers. Progress: lines `##WB step <text>` and `##WB progress <n>` on stdout.
# Source data is only read: everything is written under --out (cell names are <sample_id>_<barcode>).

SCRIPT_VERSION <- "1.0"
COUNT_METRICS <- c("nCount_RNA", "nFeature_RNA", "nCount_ATAC", "nFeature_ATAC")
DEFAULT_FLOORS <- c(nFeature_RNA = 200, nCount_RNA = 500, nFeature_ATAC = 250, nCount_ATAC = 500)
DEFAULT_LOWER_CAPS <- c(nFeature_RNA = 1000, nCount_RNA = 1000, nFeature_ATAC = 500, nCount_ATAC = 1000)
DEFAULT_UPPER_CAPS <- c(nFeature_RNA = 10000, nCount_RNA = 50000, nFeature_ATAC = 20000, nCount_ATAC = 50000)
LABEL_PREFERENCE <- c("celltype", "cell_type", "predicted.id", "celltype_harmony", "cluster_type", "clustertype",
                      "annotation", "cell.type", "labels")
OPTION_DEFAULTS <- list(
  mode = NA, stage = "full", samples = NA, out = NA, genome = "auto", thresholds = NA, nmad = "3",
  reference = NA, "ref-label" = "auto", "ref-min-score" = "0.5", "ref-max-cells" = "60000",
  integrate = "harmony", resolution = "0.4,0.8", npcs = "50", dims = "1:40", formats = "rds", threads = "4",
  name = "analysis", doublets = "none", gtf = NA, floors = "", "lower-caps" = "", "upper-caps" = "",
  "tss-min" = "2", "nucleosome-max" = "2", "frip-min" = "10", "mt-floor" = "5", "mt-cap" = "20",
  "max-cells" = NA, seed = "20261105", "gene-activity" = "yes", python = NA,
  "inspect-reference" = NA, "self-test" = NA, help = NA)

options(warn = 1, scipen = 10, stringsAsFactors = FALSE, future.globals.maxSize = Inf,
        Seurat.object.assay.version = "v3")

# ------------------------------------------------------------------ logging and errors

wb_step <- function(...) { cat("##WB step ", ..., "\n", sep = ""); flush(stdout()) }
wb_progress <- function(n) { cat("##WB progress ", round(n), "\n", sep = ""); flush(stdout()) }
say <- function(...) { cat(format(Sys.time(), "%H:%M:%S"), " ", ..., "\n", sep = ""); flush(stdout()) }
fail <- function(...) stop(structure(class = c("wb_error", "error", "condition"),
                                     list(message = paste0(...), call = NULL)))
notes <- character()
note <- function(...) { m <- paste0(...); notes <<- c(notes, m); say("NOTE: ", m) }
has_pkg <- function(p) requireNamespace(p, quietly = TRUE)

# ------------------------------------------------------------------ command line

parse_args <- function(args) {
  opt <- list(); i <- 1
  while (i <= length(args)) {
    a <- args[i]
    if (!startsWith(a, "--")) fail("Unexpected argument '", a, "' (options are --key value)")
    key <- sub("^--", "", a); val <- NULL
    if (grepl("=", key, fixed = TRUE)) { val <- sub("^[^=]*=", "", key); key <- sub("=.*$", "", key) }
    if (!key %in% names(OPTION_DEFAULTS)) fail("Unknown option --", key, " (see --help)")
    if (is.null(val)) {
      if (i < length(args) && !startsWith(args[i + 1], "--")) { val <- args[i + 1]; i <- i + 1 } else val <- "true"
    }
    opt[[key]] <- val; i <- i + 1
  }
  for (k in names(OPTION_DEFAULTS)) if (is.null(opt[[k]])) opt[[k]] <- OPTION_DEFAULTS[[k]]
  opt
}

num_opt <- function(opt, key, min = -Inf) {
  v <- suppressWarnings(as.numeric(opt[[key]]))
  if (length(v) != 1 || is.na(v) || v < min) fail("--", key, " must be a number", if (is.finite(min)) paste0(" >= ", min))
  v
}

parse_dims <- function(s) {
  s <- gsub("\\s", "", s)
  if (grepl("^[0-9]+:[0-9]+$", s)) { p <- as.integer(strsplit(s, ":")[[1]]); d <- seq(p[1], p[2]) }
  else if (grepl("^[0-9]+(,[0-9]+)*$", s)) { d <- as.integer(strsplit(s, ",")[[1]]); if (length(d) == 1) d <- seq_len(d) }
  else fail("--dims must look like 1:40, 40 or 1,2,3 (got '", s, "')")
  if (!length(d) || any(d < 1)) fail("--dims must be positive")
  sort(unique(d))
}

# "nFeature_RNA=200,nCount_RNA=500" over the defaults
parse_named <- function(s, defaults, key) {
  out <- defaults
  if (is.na(s) || !nzchar(s)) return(out)
  for (item in strsplit(s, ",")[[1]]) {
    kv <- strsplit(item, "=")[[1]]
    if (length(kv) != 2 || !kv[1] %in% names(defaults) || is.na(suppressWarnings(as.numeric(kv[2]))))
      fail("--", key, " takes metric=value pairs for ", paste(names(defaults), collapse = ", "), " (got '", item, "')")
    out[[kv[1]]] <- as.numeric(kv[2])
  }
  out
}

usage <- function() cat(
"Usage:
  Rscript sc_analysis.R --mode gex|atac|multiome --stage qc|full --samples samples.tsv --out DIR
     [--genome hg38|mm10|mm39|auto] [--thresholds thresholds.tsv] [--nmad 3]
     [--reference ref.rds] [--ref-label auto|<column>] [--ref-min-score 0.5] [--ref-max-cells 60000]
     [--integrate harmony|none] [--resolution 0.4,0.8] [--npcs 50] [--dims 1:40]
     [--formats rds,h5ad|h5mu] [--threads N] [--name PREFIX] [--doublets none|scDblFinder]
     [--gtf genes.gtf.gz] [--floors nFeature_RNA=200,...] [--lower-caps ...] [--upper-caps ...]
     [--tss-min 2] [--nucleosome-max 2] [--frip-min 10] [--mt-floor 5] [--mt-cap 20]
     [--gene-activity yes|no] [--seed 20261105] [--max-cells N (testing: random cells per sample)]
  Rscript sc_analysis.R --inspect-reference ref.rds
  Rscript sc_analysis.R --self-test
samples.tsv: tab-separated, header 'sample_id<TAB>path', path = a Cell Ranger outs folder (or the folder holding outs).
")

# ------------------------------------------------------------------ threshold rule (pure functions)

round_lower <- function(x) floor(x / 50 + 1e-9) * 50
round_upper <- function(x) {            # up to 2 significant figures
  if (!is.finite(x) || x <= 0) return(x)
  mag <- 10^(floor(log10(x)) - 1)
  ceiling(x / mag - 1e-9) * mag
}

# Bounds of one count metric from the reference cells' values
count_bounds <- function(values, metric, nmad, floors, lower_caps, upper_caps) {
  z <- log10(values[is.finite(values)] + 1)
  med <- stats::median(z); m <- stats::mad(z, constant = 1.4826)
  if (!length(z) || !is.finite(m) || m == 0) {
    raw_lo <- NA; raw_hi <- NA; lo <- floors[[metric]]; hi <- upper_caps[[metric]]
  } else {
    raw_lo <- 10^(med - nmad * m) - 1; raw_hi <- 10^(med + nmad * m) - 1
    lo <- min(max(raw_lo, floors[[metric]]), lower_caps[[metric]])
    hi <- min(raw_hi, upper_caps[[metric]])
  }
  list(log10_median = med, log10_mad = m, raw_lower = raw_lo, raw_upper = raw_hi,
       min = round_lower(lo), max = round_upper(hi))
}

mt_bound <- function(p, nmad, mt_floor, mt_cap) {
  p <- p[is.finite(p)]
  if (!length(p)) return(NA_real_)
  v <- stats::median(p) + nmad * stats::mad(p, constant = 1.4826)
  ceiling(min(max(mt_floor, v), mt_cap) * 100 - 1e-9) / 100
}

threshold_columns <- function(has_rna, has_atac) {
  cols <- c("sample_id", "cells_total")
  for (m in COUNT_METRICS) if ((has_rna && grepl("RNA", m)) || (has_atac && grepl("ATAC", m)))
    cols <- c(cols, paste0(m, "_min"), paste0(m, "_max"))
  if (has_rna) cols <- c(cols, "percent_mt_max")
  if (has_atac) cols <- c(cols, "TSS_min", "nucleosome_max", "FRiP_min")
  c(cols, "cells_pass")
}

# Per-cell pass / failure reasons; bounds NA = no bound. Counts exclusive, percent.mt inclusive.
apply_thresholds <- function(metrics, th) {
  th <- th[match(metrics$sample_id, th$sample_id), , drop = FALSE]
  reasons <- list()
  add <- function(name, bad) { bad[is.na(bad)] <- TRUE; reasons[[name]] <<- bad }
  chk <- function(col, bound, cmp, name) {
    if (!col %in% names(metrics) || !bound %in% names(th)) return()
    b <- suppressWarnings(as.numeric(th[[bound]])); v <- metrics[[col]]; has <- !is.na(b)
    bad <- rep(FALSE, nrow(metrics))
    bad[has] <- !(is.finite(v[has]) & cmp(v[has], b[has]))
    add(name, bad)
  }
  for (m in COUNT_METRICS) {
    chk(m, paste0(m, "_min"), `>`, paste0("low_", m)); chk(m, paste0(m, "_max"), `<`, paste0("high_", m))
  }
  chk("percent.mt", "percent_mt_max", `<=`, "high_percent_mt")
  chk("TSS.enrichment", "TSS_min", `>`, "low_TSS")
  chk("nucleosome_signal", "nucleosome_max", `<`, "high_nucleosome")
  chk("FRiP", "FRiP_min", `>`, "low_FRiP")
  reasons <- as.data.frame(reasons, check.names = FALSE)
  if (!length(reasons)) reasons <- data.frame(row.names = seq_len(nrow(metrics)))
  pass <- if (ncol(reasons)) !Reduce(`|`, reasons) else rep(TRUE, nrow(metrics))
  why <- if (ncol(reasons)) apply(as.matrix(reasons), 1, function(r) paste(names(reasons)[r], collapse = ";")) else rep("", nrow(metrics))
  list(pass = pass, reasons = reasons, why = why)
}

propose_thresholds <- function(metrics, P) {
  has_rna <- "nCount_RNA" %in% names(metrics); has_atac <- "nCount_ATAC" %in% names(metrics)
  rows <- list(); deriv <- list()
  for (s in unique(metrics$sample_id)) {
    d <- metrics[metrics$sample_id == s, , drop = FALSE]
    row <- list(sample_id = s, cells_total = nrow(d))
    ok <- rep(TRUE, nrow(d))
    if (has_rna) {
      row$percent_mt_max <- if (isTRUE(P$has_mito)) mt_bound(d$percent.mt, P$nmad, P$mt_floor, P$mt_cap) else NA
      if (!is.na(row$percent_mt_max)) ok <- ok & d$percent.mt <= row$percent_mt_max
    }
    if (has_atac) {
      row$TSS_min <- P$tss_min; row$nucleosome_max <- P$nucleosome_max; row$FRiP_min <- P$frip_min
      ok <- ok & d$TSS.enrichment > P$tss_min & d$nucleosome_signal < P$nucleosome_max & d$FRiP > P$frip_min
    }
    ok[is.na(ok)] <- FALSE
    # count bounds come from cells passing the quality criteria (as in threshold_derivation.tsv)
    ref <- if (sum(ok) >= 30) ok else rep(TRUE, nrow(d))
    if (sum(ok) < 30) say("  ", s, ": only ", sum(ok), " cells pass the quality criteria; count bounds use all cells")
    for (m in COUNT_METRICS) if (m %in% names(d)) {
      b <- count_bounds(d[[m]][ref], m, P$nmad, P$floors, P$lower_caps, P$upper_caps)
      row[[paste0(m, "_min")]] <- b$min; row[[paste0(m, "_max")]] <- b$max
      deriv[[length(deriv) + 1]] <- data.frame(sample_id = s, metric = m, reference_cells = sum(ref),
        log10_median = b$log10_median, log10_normalized_MAD = b$log10_mad, raw_lower = b$raw_lower,
        raw_upper = b$raw_upper, lower_information_floor = P$floors[[m]], lower_threshold_cap = P$lower_caps[[m]],
        upper_threshold_cap = P$upper_caps[[m]], lower_exclusive = b$min, upper_exclusive = b$max)
    }
    rows[[s]] <- row
  }
  cols <- threshold_columns(has_rna, has_atac)
  th <- do.call(rbind, lapply(rows, function(r) {
    r$cells_pass <- NA; as.data.frame(r[cols[cols %in% names(r)]], check.names = FALSE) }))
  th <- th[, cols, drop = FALSE]
  th$cells_pass <- as.integer(tapply(apply_thresholds(metrics, th)$pass, factor(metrics$sample_id, th$sample_id), sum))
  rownames(th) <- NULL
  list(thresholds = th, derivation = do.call(rbind, deriv))
}

read_thresholds <- function(path, metrics) {
  th <- utils::read.delim(path, check.names = FALSE, na.strings = c("", "NA", "NaN", "na"), colClasses = "character")
  if (!"sample_id" %in% names(th)) fail("Thresholds file ", path, " has no sample_id column")
  samples <- unique(metrics$sample_id)
  miss <- setdiff(samples, th$sample_id)
  if (length(miss)) fail("Thresholds file ", path, " has no row for sample(s): ", paste(miss, collapse = ", "))
  extra <- setdiff(th$sample_id, samples)
  if (length(extra)) say("  ignoring threshold rows for unknown samples: ", paste(extra, collapse = ", "))
  th <- th[th$sample_id %in% samples, , drop = FALSE]
  for (c in setdiff(names(th), "sample_id")) {
    v <- suppressWarnings(as.numeric(th[[c]]))
    if (any(!is.na(th[[c]]) & is.na(v))) fail("Thresholds file: column ", c, " has non-numeric values")
    th[[c]] <- v
  }
  wanted <- setdiff(threshold_columns("nCount_RNA" %in% names(metrics), "nCount_ATAC" %in% names(metrics)),
                    c("sample_id", "cells_total", "cells_pass"))
  absent <- setdiff(wanted, names(th))
  if (length(absent)) note("Thresholds file has no column(s) ", paste(absent, collapse = ", "), ": no bound applied")
  th
}

# ------------------------------------------------------------------ inputs

resolve_sample <- function(id, path, mode) {
  p <- normalizePath(path.expand(path), mustWork = FALSE)
  if (!dir.exists(p)) fail("Sample ", id, ": folder not found: ", path)
  if (dir.exists(file.path(p, "outs"))) p <- file.path(p, "outs")
  f <- function(...) file.path(p, ...)
  s <- list(id = id, outs = p)
  if (file.exists(f("filtered_feature_bc_matrix.h5")) && file.exists(f("atac_fragments.tsv.gz"))) {
    s$type <- "arc"; s$h5 <- f("filtered_feature_bc_matrix.h5"); s$fragments <- f("atac_fragments.tsv.gz")
    s$metrics <- if (file.exists(f("per_barcode_metrics.csv"))) f("per_barcode_metrics.csv") else NA
  } else if (file.exists(f("filtered_peak_bc_matrix.h5")) && file.exists(f("fragments.tsv.gz"))) {
    s$type <- "atac"; s$h5 <- f("filtered_peak_bc_matrix.h5"); s$fragments <- f("fragments.tsv.gz")
    s$metrics <- if (file.exists(f("singlecell.csv"))) f("singlecell.csv") else NA
  } else if (file.exists(f("filtered_feature_bc_matrix.h5"))) {
    s$type <- "gex"; s$h5 <- f("filtered_feature_bc_matrix.h5")
  } else if (file.exists(f("sample_filtered_feature_bc_matrix.h5"))) {          # cellranger multi, per sample
    s$type <- "gex"; s$h5 <- f("sample_filtered_feature_bc_matrix.h5")
  } else if (dir.exists(f("per_sample_outs"))) {
    hits <- Sys.glob(f("per_sample_outs", "*", "count", "sample_filtered_feature_bc_matrix.h5"))
    if (length(hits) != 1) fail("Sample ", id, ": cellranger multi output with ", length(hits),
                                " samples; give one per_sample_outs/<sample>/count folder per row")
    s$type <- "gex"; s$h5 <- hits
  } else fail("Sample ", id, ": no Cell Ranger output in ", p, " (expected filtered_feature_bc_matrix.h5, or ",
              "filtered_peak_bc_matrix.h5 + fragments.tsv.gz for cellranger-atac)")
  ok <- switch(mode, gex = s$type %in% c("gex", "arc"), atac = s$type %in% c("atac", "arc"), multiome = s$type == "arc")
  if (!ok) fail("Sample ", id, ": ", p, " is a ", c(gex = "cellranger count (gene expression)", atac = "cellranger-atac",
                arc = "cellranger-arc (multiome)")[[s$type]], " output; --mode ", mode, " needs ",
                c(gex = "gene expression (cellranger count or arc)", atac = "ATAC (cellranger-atac or arc) with a fragment file",
                  multiome = "cellranger-arc outs (filtered_feature_bc_matrix.h5, atac_fragments.tsv.gz, per_barcode_metrics.csv)")[[mode]])
  if (mode == "gex" && s$type == "arc") say("  ", id, ": multiome run, using its gene expression only")
  if (mode != "gex") {
    if (!file.exists(paste0(s$fragments, ".tbi"))) fail("Sample ", id, ": fragment index missing: ", s$fragments, ".tbi")
    if (is.na(s$metrics)) say("  ", id, ": no per-barcode metrics CSV; FRiP is estimated from the fragment file")
  }
  s
}

read_samples <- function(path, mode) {
  if (is.na(path) || !file.exists(path)) fail("--samples file not found: ", path)
  tab <- utils::read.delim(path, colClasses = "character", check.names = FALSE, strip.white = TRUE)
  if (!all(c("sample_id", "path") %in% names(tab))) fail("--samples must have the header 'sample_id<TAB>path'")
  tab <- tab[nzchar(tab$sample_id) | nzchar(tab$path), , drop = FALSE]
  if (!nrow(tab)) fail("--samples lists no samples")
  bad <- tab$sample_id[!grepl("^[A-Za-z0-9][A-Za-z0-9._-]*$", tab$sample_id)]
  if (length(bad)) fail("Sample ids may contain letters, digits, '.', '_' and '-' only: ", paste(bad, collapse = ", "))
  if (anyDuplicated(tab$sample_id)) fail("Duplicated sample ids in --samples")
  lapply(seq_len(nrow(tab)), function(i) resolve_sample(tab$sample_id[i], tab$path[i], mode))
}

# Genome name stored in a 10x HDF5 matrix (e.g. GRCh38, mm10, GRCm39), or NA
h5_genome <- function(path) {
  tryCatch({
    f <- hdf5r::H5File$new(path, mode = "r"); on.exit(f$close_all())
    g <- if (f$exists("matrix/features/genome")) f[["matrix/features/genome"]][] else names(f)[1]
    u <- unique(g); u[1]
  }, error = function(e) NA_character_)
}

# Gene expression feature names of a 10x HDF5 matrix (v3 features/ or v2 <genome>/gene_names)
h5_gene_names <- function(path) {
  f <- hdf5r::H5File$new(path, mode = "r"); on.exit(f$close_all())
  if (f$exists("matrix/features/name")) {
    n <- f[["matrix/features/name"]][]
    t <- if (f$exists("matrix/features/feature_type")) f[["matrix/features/feature_type"]][] else rep("Gene Expression", length(n))
    return(n[t == "Gene Expression"])
  }
  g <- names(f)[1]
  if (f$exists(paste0(g, "/gene_names"))) f[[paste0(g, "/gene_names")]][] else character()
}

genome_from_name <- function(g) {
  if (is.na(g)) return(NA_character_)
  if (grepl("GRCm39|mm39", g, ignore.case = TRUE)) return("mm39")
  if (grepl("GRCm38|mm10", g, ignore.case = TRUE)) return("mm10")
  if (grepl("GRCh38|hg38", g, ignore.case = TRUE)) return("hg38")
  if (grepl("GRCh37|hg19", g, ignore.case = TRUE)) return("hg19")
  NA_character_
}

# The reference GTF of the Cell Ranger run (reference_path in _invocation next to outs/), or NA
invocation_gtf <- function(outs) {
  inv <- file.path(dirname(outs), "_invocation")
  if (!file.exists(inv)) return(NA_character_)
  m <- regmatches(txt <- paste(readLines(inv, warn = FALSE), collapse = "\n"),
                  regexpr('reference_path\\s*=\\s*"[^"]+"', txt))
  if (!length(m)) return(NA_character_)
  ref <- sub('^[^"]*"', "", sub('"$', "", m))
  parts <- strsplit(sub("^/", "", ref), "/")[[1]]
  cands <- c(ref, if (length(parts) > 1) file.path("/nfs", tolower(parts[1]), paste(parts[-1], collapse = "/")))
  for (r in cands) for (g in c("genes/genes.gtf.gz", "genes/genes.gtf")) if (file.exists(file.path(r, g))) return(file.path(r, g))
  NA_character_
}

# ------------------------------------------------------------------ gene annotation (ATAC)

match_seqstyle <- function(gr, peak_names) {
  use_chr <- mean(grepl("^chr", utils::head(peak_names, 2000))) > 0.5
  old <- GenomeInfoDb::seqlevels(gr)
  new <- if (use_chr) ifelse(grepl("^chr", old), old, ifelse(old %in% c("MT", "M"), "chrM", paste0("chr", old)))
         else ifelse(old == "chrM", "MT", sub("^chr", "", old))
  if (anyDuplicated(new)) return(gr)
  GenomeInfoDb::renameSeqlevels(gr, stats::setNames(new, old))
}

load_annotation <- function(genome, gtf) {
  if (!is.na(gtf)) {
    say("  gene annotation from ", gtf)
    g <- rtracklayer::import(gtf, format = "gtf", feature.type = "exon")
    mc <- S4Vectors::mcols(g)
    bt <- if ("gene_biotype" %in% names(mc)) mc$gene_biotype else if ("gene_type" %in% names(mc)) mc$gene_type else "protein_coding"
    S4Vectors::mcols(g) <- data.frame(tx_id = as.character(mc$transcript_id), gene_name = as.character(mc$gene_name),
                                      gene_id = as.character(mc$gene_id), gene_biotype = as.character(bt), type = "exon")
    g <- g[!is.na(g$gene_name) & nzchar(g$gene_name)]
    return(g)
  }
  db <- switch(genome, hg38 = "EnsDb.Hsapiens.v86", mm10 = "EnsDb.Mmusculus.v79", NA)
  if (is.na(db)) fail("No gene annotation for genome '", genome, "': give --gtf <genes.gtf.gz> of the Cell Ranger reference ",
                      "(EnsDb annotations are installed for hg38 and mm10 only)")
  if (!has_pkg(db)) fail("Package ", db, " is not installed (gene annotation for ", genome, ")")
  say("  gene annotation from ", db)
  suppressWarnings(Signac::GetGRangesFromEnsDb(ensdb = getExportedValue(db, db), verbose = FALSE))
}

# ------------------------------------------------------------------ QC stage

std_chrom <- function(chr) grepl("^(chr)?([0-9]+|X|Y)$", chr)

read_10x <- function(s) {
  m <- suppressWarnings(Seurat::Read10X_h5(s$h5, use.names = TRUE))
  if (!is.list(m)) m <- if (s$type == "atac") list(Peaks = m) else list(`Gene Expression` = m)
  m
}

fragment_frip <- function(s, barcodes, n_count_atac) {
  if (!is.na(s$metrics)) {
    sel <- if (s$type == "arc") c("barcode", "atac_fragments", "atac_peak_region_fragments")
           else c("barcode", "passed_filters", "peak_region_fragments")
    mt <- if (has_pkg("data.table")) as.data.frame(data.table::fread(s$metrics, select = sel, showProgress = FALSE))
          else utils::read.csv(s$metrics)[, sel]
    if (!all(sel %in% names(mt))) fail("Sample ", s$id, ": ", s$metrics, " lacks columns ", paste(sel, collapse = ", "))
    i <- match(barcodes, mt$barcode)
    if (anyNA(i)) fail("Sample ", s$id, ": ", sum(is.na(i)), " cells are missing from ", s$metrics)
    return(list(FRiP = 100 * mt[[sel[3]]][i] / pmax(mt[[sel[2]]][i], 1), fragments = mt[[sel[2]]][i]))
  }
  # no metrics CSV: fragments per barcode from the fragment file; peak counts are cut sites (2 per fragment)
  cf <- Signac::CountFragments(s$fragments, cells = barcodes, verbose = FALSE)
  tot <- cf$frequency_count[match(barcodes, cf$CB)]; tot[is.na(tot)] <- 0
  list(FRiP = pmin(100, 100 * n_count_atac / 2 / pmax(tot, 1)), fragments = tot)
}

qc_sample <- function(s, mode, ctx, P) {
  m <- read_10x(s)
  need_rna <- mode %in% c("gex", "multiome"); need_atac <- mode %in% c("atac", "multiome")
  rna <- if (need_rna) m[["Gene Expression"]] else NULL
  peaks <- if (need_atac) m[["Peaks"]] else NULL
  if (need_rna && is.null(rna)) fail("Sample ", s$id, ": ", s$h5, " has no Gene Expression matrix")
  if (need_atac && is.null(peaks)) fail("Sample ", s$id, ": ", s$h5, " has no Peaks matrix")
  if (need_rna && length(setdiff(names(m), c("Gene Expression", "Peaks"))))
    say("  ", s$id, ": using Gene Expression only (also has ", paste(setdiff(names(m), c("Gene Expression", "Peaks")), collapse = ", "), ")")
  rm(m)
  barcodes <- colnames(if (need_rna) rna else peaks)
  if (!is.na(P$max_cells) && length(barcodes) > P$max_cells) {
    set.seed(P$seed); barcodes <- sort(sample(barcodes, P$max_cells))
    say("  ", s$id, ": testing with ", P$max_cells, " random cells (--max-cells)")
  }
  cells <- paste0(s$id, "_", barcodes)
  meta <- data.frame(barcode = barcodes, sample_id = s$id, row.names = cells)
  out <- list(id = s$id, type = s$type, outs = s$outs, h5 = s$h5, fragments = s$fragments,
              barcodes = stats::setNames(barcodes, cells))
  if (need_rna) {
    rna <- rna[, barcodes, drop = FALSE]; colnames(rna) <- cells
    meta$nCount_RNA <- Matrix::colSums(rna); meta$nFeature_RNA <- Matrix::colSums(rna > 0)
    mito <- grep(ctx$mito_pattern, rownames(rna))
    meta$percent.mt <- if (length(mito)) 100 * Matrix::colSums(rna[mito, , drop = FALSE]) / pmax(meta$nCount_RNA, 1) else 0
    out$rna <- rna
  }
  if (need_atac) {
    peaks <- peaks[, barcodes, drop = FALSE]
    gr <- Signac::StringToGRanges(rownames(peaks), sep = c(":", "-"))
    keep <- std_chrom(as.character(GenomicRanges::seqnames(gr)))
    peaks <- peaks[keep, , drop = FALSE]; rownames(peaks) <- Signac::GRangesToString(gr[keep]); colnames(peaks) <- cells
    frag <- Signac::CreateFragmentObject(s$fragments, cells = out$barcodes, validate.fragments = FALSE, verbose = FALSE)
    annotation <- match_seqstyle(ctx$annotation, rownames(peaks))
    ca <- suppressWarnings(Signac::CreateChromatinAssay(counts = peaks, sep = c("-", "-"), fragments = frag,
                                                       annotation = annotation, verbose = FALSE))
    tmp <- suppressWarnings(SeuratObject::CreateSeuratObject(ca, assay = "ATAC"))
    say("  ", s$id, ": nucleosome signal")
    tmp <- Signac::NucleosomeSignal(tmp, n = ncol(tmp) * 5000, verbose = FALSE)
    say("  ", s$id, ": TSS enrichment")
    tmp <- Signac::TSSEnrichment(tmp, fast = TRUE, verbose = FALSE)
    meta$nCount_ATAC <- tmp$nCount_ATAC; meta$nFeature_ATAC <- tmp$nFeature_ATAC
    meta$TSS.enrichment <- tmp$TSS.enrichment; meta$nucleosome_signal <- tmp$nucleosome_signal
    fr <- fragment_frip(s, barcodes, meta$nCount_ATAC)
    meta$FRiP <- fr$FRiP; meta$atac_fragments <- fr$fragments
    bl <- switch(ctx$genome, hg38 = Signac::blacklist_hg38_unified, mm10 = Signac::blacklist_mm10, NULL)
    if (!is.null(bl)) meta$blacklist_ratio <- tryCatch(
      Signac::FractionCountsInRegion(tmp, assay = "ATAC", regions = match_seqstyle(bl, rownames(peaks))),
      error = function(e) NA_real_)
    out$peaks <- peaks; out$annotation <- annotation
    rm(tmp, ca)
  }
  out$meta <- meta
  out
}

density_panel <- function(x, pass, lo, hi, logscale, title, xlab, first) {
  ok <- is.finite(x); x <- x[ok]; pass <- pass[ok]
  tr <- if (logscale) function(v) log10(v + 1) else identity
  z <- tr(x); cuts <- tr(c(lo, hi)); cuts <- cuts[is.finite(cuts)]
  if (length(unique(z)) < 2) { plot.new(); title(main = title); return() }
  rng <- stats::quantile(z, c(0.002, 0.998)); rng <- range(c(rng, cuts))
  pad <- diff(rng) * 0.05; rng <- rng + c(-pad, pad)
  d_all <- stats::density(z, n = 512)
  d_ok <- if (sum(pass) >= 2 && length(unique(z[pass])) > 1) stats::density(z[pass], n = 512) else NULL
  ymax <- max(d_all$y, if (!is.null(d_ok)) d_ok$y * mean(pass))
  plot(d_all, xlim = rng, ylim = c(0, ymax * 1.08), main = title, xlab = xlab, ylab = "Density", lwd = 2.5,
       col = "#8c8c8c", zero.line = FALSE)
  if (!is.null(d_ok)) lines(d_ok$x, d_ok$y * mean(pass), col = "#1878a0", lwd = 2.5)
  abline(v = cuts, col = "#c34a36", lty = 2, lwd = 2.5)
}

fmt <- function(x) format(x, big.mark = ",", scientific = FALSE, trim = TRUE)
fmt_b <- function(x) if (is.na(x)) "none" else fmt(signif(x, 6))

open_png <- function(file, width, height) {
  grDevices::png(file, width = width, height = height, units = "in", res = 130, type = "cairo", family = "Arial", bg = "white")
}

# Every figure is drawn three times: the PNG the job's page shows, and SVG and PDF (vector) beside it with the same
# name. draw() makes the plot on the open device. A failed SVG or PDF is reported and skipped; a failed PNG stops.
draw_figure <- function(file, width, height, draw) {
  base <- sub("\\.png$", "", file)
  devices <- list(
    png = function() open_png(file, width, height),
    svg = function() if (requireNamespace("svglite", quietly = TRUE))
        svglite::svglite(paste0(base, ".svg"), width = width, height = height, bg = "white", system_fonts = list(sans = "Arial"))
      else grDevices::svg(paste0(base, ".svg"), width = width, height = height, family = "Arial", bg = "white"),
    pdf = function() if (capabilities("cairo"))
        grDevices::cairo_pdf(paste0(base, ".pdf"), width = width, height = height, family = "Arial", bg = "white")
      else grDevices::pdf(paste0(base, ".pdf"), width = width, height = height, family = "Helvetica", bg = "white", useDingbats = FALSE))
  for (kind in names(devices)) {
    ok <- tryCatch({
      devices[[kind]]()
      tryCatch(draw(), finally = grDevices::dev.off())
      TRUE
    }, error = function(e) { if (kind == "png") stop(e); say("  ", basename(base), ".", kind, " not written: ", conditionMessage(e)); FALSE })
  }
  invisible(file)
}

plot_qc <- function(d, th, pass, file) {
  t <- as.list(th)
  panels <- list()
  for (m in COUNT_METRICS) if (m %in% names(d))
    panels[[m]] <- list(m, t[[paste0(m, "_min")]], t[[paste0(m, "_max")]], TRUE,
                        sprintf("%s: %s < x < %s", m, fmt_b(t[[paste0(m, "_min")]]), fmt_b(t[[paste0(m, "_max")]])),
                        sprintf("log10(%s + 1)", m))
  if ("percent.mt" %in% names(d)) panels$mt <- list("percent.mt", NA, t$percent_mt_max, FALSE,
                                                    sprintf("percent.mt <= %s", fmt_b(t$percent_mt_max)), "Mitochondrial UMIs (%)")
  if ("TSS.enrichment" %in% names(d)) {
    panels$tss <- list("TSS.enrichment", t$TSS_min, NA, FALSE, sprintf("TSS enrichment > %s", fmt_b(t$TSS_min)), "TSS enrichment")
    panels$nuc <- list("nucleosome_signal", NA, t$nucleosome_max, FALSE,
                       sprintf("Nucleosome signal < %s", fmt_b(t$nucleosome_max)), "Nucleosome signal")
    panels$frip <- list("FRiP", t$FRiP_min, NA, FALSE, sprintf("FRiP > %s%%", fmt_b(t$FRiP_min)), "Fragments in peaks (%)")
  }
  nc <- if (length(panels) <= 4) length(panels) else if (length(panels) == 8) 4 else 3; nr <- ceiling(length(panels) / nc)
  draw_figure(file, 4.6 * nc, 4 * nr + 1.1, function() {
  graphics::par(mfrow = c(nr, nc), mar = c(4.6, 4.6, 3.2, 1), oma = c(0, 0, 4.6, 0), cex = 1.05,
                cex.main = 1.1, cex.lab = 1.05, cex.axis = 0.95)
  for (i in seq_along(panels)) { p <- panels[[i]]
    density_panel(d[[p[[1]]]], pass, p[[2]], p[[3]], p[[4]], p[[5]], p[[6]], i == 1) }
  graphics::mtext(sprintf("%s: proposed QC thresholds, %s of %s cells pass", th$sample_id, fmt(sum(pass)), fmt(length(pass))),
                  outer = TRUE, line = 2.4, cex = 1.45, font = 2)
  graphics::par(fig = c(0, 1, 0, 1), oma = c(0, 0, 0, 0), mar = c(0, 0, 0, 0), new = TRUE)
  graphics::plot.new(); graphics::plot.window(c(0, 1), c(0, 1), xaxs = "i", yaxs = "i")
  graphics::legend(0.5, 1 - 0.8 / (4 * nr + 1.1), xjust = 0.5, yjust = 0.5, horiz = TRUE, bty = "n", lty = c(1, 1, 2), lwd = 2.5, cex = 1.05,
                   col = c("#8c8c8c", "#1878a0", "#c34a36"), c(sprintf("All cells (%s)", fmt(length(pass))),
                   sprintf("Cells passing every threshold (%s)", fmt(sum(pass))), "Proposed threshold"))
  })
}

write_tsv <- function(x, path) {
  con <- if (grepl("\\.gz$", path)) gzfile(path, "wt") else file(path, "wt")
  on.exit(close(con))
  utils::write.table(x, con, sep = "\t", quote = FALSE, row.names = FALSE, na = "NA")
}

save_rds_atomic <- function(x, path, compress = TRUE) {
  tmp <- paste0(path, ".tmp")
  con <- if (compress) gzfile(tmp, "wb", compression = 1) else file(tmp, "wb")
  tryCatch(saveRDS(x, con), finally = close(con))
  if (!file.rename(tmp, path)) fail("Could not move ", tmp, " to ", path)
}

qc_summary_table <- function(metrics, th, res) {
  do.call(rbind, lapply(th$sample_id, function(s) {
    i <- metrics$sample_id == s
    r <- data.frame(sample_id = s, cells_total = sum(i), cells_pass = sum(res$pass[i]),
                    percent_pass = round(100 * mean(res$pass[i]), 1))
    for (m in intersect(c(COUNT_METRICS, "percent.mt", "TSS.enrichment", "nucleosome_signal", "FRiP"), names(metrics)))
      r[[paste0("median_", m)]] <- signif(stats::median(metrics[[m]][i], na.rm = TRUE), 4)
    for (k in names(res$reasons)) r[[paste0("fail_", k)]] <- sum(res$reasons[[k]][i])
    r
  }))
}

run_qc <- function(samples, mode, ctx, P, out) {
  pieces <- list(); n <- length(samples)
  for (k in seq_along(samples)) {
    s <- samples[[k]]
    wb_step(sprintf("QC metrics: %s (%d of %d)", s$id, k, n)); wb_progress(5 + 25 * (k - 1) / n)
    pieces[[s$id]] <- qc_sample(s, mode, ctx, P)
    say("  ", s$id, ": ", fmt(nrow(pieces[[s$id]]$meta)), " cells")
  }
  metrics <- bind_rows_fill(lapply(pieces, `[[`, "meta"))
  qc <- list(key = ctx$key, mode = mode, genome = ctx$genome, mito_pattern = ctx$mito_pattern,
             has_mito = ctx$has_mito, pieces = pieces, metrics = metrics, version = SCRIPT_VERSION)
  write_qc_outputs(qc, P, out)
  wb_step("Saving QC checkpoint")
  save_rds_atomic(qc, file.path(out, "checkpoints", "qc_object.rds"), compress = FALSE)
  qc
}

write_qc_outputs <- function(qc, P, out) {
  wb_step("Proposing QC thresholds")
  metrics <- qc$metrics
  prop <- propose_thresholds(metrics, P)
  res <- apply_thresholds(metrics, prop$thresholds)
  write_tsv(metrics, file.path(out, "tables", "qc_metrics.tsv.gz"))
  write_tsv(prop$thresholds, file.path(out, "tables", "qc_thresholds_proposed.tsv"))
  write_tsv(prop$derivation, file.path(out, "tables", "qc_threshold_derivation.tsv"))
  write_tsv(qc_summary_table(metrics, prop$thresholds, res), file.path(out, "tables", "qc_summary.tsv"))
  wb_step("QC figures")
  for (i in seq_len(nrow(prop$thresholds))) {
    s <- prop$thresholds$sample_id[i]; sel <- metrics$sample_id == s
    plot_qc(metrics[sel, , drop = FALSE], prop$thresholds[i, , drop = FALSE], res$pass[sel],
            file.path(out, "figures", paste0("qc_", s, ".png")))
  }
  print(prop$thresholds, row.names = FALSE)
  prop
}

bind_rows_fill <- function(dfs) {
  cols <- unique(unlist(lapply(dfs, names)))
  dfs <- lapply(dfs, function(d) { for (c in setdiff(cols, names(d))) d[[c]] <- NA; d[, cols, drop = FALSE] })
  out <- do.call(rbind, unname(dfs))
  out
}

# ------------------------------------------------------------------ building the filtered object

cbind_union <- function(mats) {
  genes <- unique(unlist(lapply(mats, rownames)))
  if (all(vapply(mats, function(m) identical(rownames(m), genes), TRUE))) return(do.call(cbind, mats))
  do.call(cbind, lapply(mats, function(m) {
    t <- methods::as(m, "TsparseMatrix")
    Matrix::sparseMatrix(i = match(rownames(m), genes)[t@i + 1], j = t@j + 1, x = t@x,
                         dims = c(length(genes), ncol(m)), dimnames = list(genes, colnames(m)))
  }))
}

with_workers <- function(n, expr) {
  if (n > 1 && future::supportsMulticore()) {
    old <- future::plan(future::multicore, workers = n); on.exit(future::plan(old), add = TRUE)
  }
  force(expr)
}

build_object <- function(qc, keep, mode, P) {
  pieces <- lapply(qc$pieces, function(p) {
    cells <- intersect(rownames(p$meta), keep)
    if (!length(cells)) return(NULL)
    p$cells <- cells; p
  })
  pieces <- Filter(Negate(is.null), pieces)
  meta <- bind_rows_fill(lapply(pieces, function(p) p$meta[p$cells, , drop = FALSE]))
  # QC-time values keep their names with a qc_ prefix: the assays recompute nCount_* / nFeature_*
  for (m in intersect(COUNT_METRICS, names(meta))) { meta[[paste0("qc_", m)]] <- meta[[m]]; meta[[m]] <- NULL }
  obj <- NULL
  if (mode %in% c("gex", "multiome")) {
    counts <- cbind_union(lapply(pieces, function(p) p$rna[, p$cells, drop = FALSE]))
    obj <- SeuratObject::CreateSeuratObject(counts, assay = "RNA", meta.data = meta, project = P$name)
  }
  if (mode %in% c("atac", "multiome")) {
    frags <- lapply(pieces, function(p) Signac::CreateFragmentObject(p$fragments, cells = p$barcodes[p$cells],
                                                                     validate.fragments = FALSE, verbose = FALSE))
    if (length(pieces) == 1) {
      peaks <- pieces[[1]]$peaks[, pieces[[1]]$cells, drop = FALSE]
    } else {
      wb_step("Common peak set across samples (reduce), counting fragments per sample")
      gr <- Reduce(c, lapply(pieces, function(p) Signac::StringToGRanges(rownames(p$peaks), sep = c("-", "-"))))
      combined <- GenomicRanges::reduce(gr)
      w <- GenomicRanges::width(combined); combined <- combined[w < 10000 & w > 20]
      say("  ", fmt(length(combined)), " combined peaks")
      peaks <- do.call(cbind, lapply(seq_along(pieces), function(k) {
        say("  counting ", pieces[[k]]$id)
        with_workers(min(P$threads, 8), Signac::FeatureMatrix(frags[[k]], features = combined,
                                                              cells = pieces[[k]]$cells, verbose = FALSE))
      }))
      peaks <- peaks[, rownames(meta), drop = FALSE]
    }
    ca <- suppressWarnings(Signac::CreateChromatinAssay(counts = peaks, sep = c("-", "-"), fragments = unname(frags),
                                                       annotation = pieces[[1]]$annotation, verbose = FALSE))
    if (is.null(obj)) obj <- suppressWarnings(SeuratObject::CreateSeuratObject(ca, assay = "ATAC", meta.data = meta, project = P$name))
    else obj[["ATAC"]] <- ca
  }
  obj
}

# ------------------------------------------------------------------ analysis steps

run_doublets <- function(obj, P) {
  if (P$doublets == "none") return(obj)
  if (!"RNA" %in% SeuratObject::Assays(obj)) { note("Doublet detection needs RNA; skipped for ATAC-only data"); return(obj) }
  if (!has_pkg("scDblFinder") || !has_pkg("SingleCellExperiment")) {
    note("scDblFinder is not installed: doublet detection skipped"); return(obj)
  }
  wb_step("Doublet detection (scDblFinder)")
  res <- tryCatch({
    sce <- SingleCellExperiment::SingleCellExperiment(list(counts = SeuratObject::GetAssayData(obj, assay = "RNA", slot = "counts")))
    sce <- scDblFinder::scDblFinder(sce, samples = obj$sample_id)
    data.frame(class = as.character(sce$scDblFinder.class), score = sce$scDblFinder.score, row.names = colnames(sce))
  }, error = function(e) { note("scDblFinder failed (", conditionMessage(e), "): doublets kept"); NULL })
  if (is.null(res)) return(obj)
  obj$doublet_class <- res[colnames(obj), "class"]; obj$doublet_score <- res[colnames(obj), "score"]
  n <- sum(obj$doublet_class == "doublet")
  say("  removing ", fmt(n), " doublets")
  subset(obj, cells = colnames(obj)[obj$doublet_class != "doublet"])
}

add_clusters <- function(obj, graph, res, algorithm, prefix = "cluster") {
  obj <- Seurat::FindClusters(obj, graph.name = graph, resolution = res, algorithm = algorithm,
                              random.seed = P$seed, verbose = FALSE)
  for (r in res) {   # cluster_res<r> replaces Seurat's <graph>_res.<r> columns
    obj[[paste0("cluster_res", r)]] <- obj[[paste0(graph, "_res.", r)]][, 1]
    obj[[paste0(graph, "_res.", r)]] <- NULL
  }
  obj$seurat_clusters <- NULL
  obj$cluster <- obj[[paste0("cluster_res", res[1])]][, 1]
  obj
}

run_rna <- function(obj, P, multi) {
  Seurat::DefaultAssay(obj) <- "RNA"
  obj <- Seurat::NormalizeData(obj, verbose = FALSE)
  wb_step("SCTransform v2 (", if (has_pkg("glmGamPoi")) "glmGamPoi" else "without glmGamPoi", ")")
  obj <- suppressWarnings(Seurat::SCTransform(obj, vst.flavor = "v2", variable.features.n = 3000,
                                              ncells = min(5000, ncol(obj)), seed.use = P$seed, verbose = FALSE))
  npcs <- min(P$npcs, ncol(obj) - 1)
  wb_step("PCA (", npcs, " components)")
  obj <- Seurat::RunPCA(obj, npcs = npcs, seed.use = P$seed, verbose = FALSE)
  dims <- P$dims[P$dims <= npcs]
  red <- "pca"
  if (multi && P$integrate == "harmony") {
    wb_step("Harmony by sample on PCA")
    obj <- run_harmony(obj, "pca", dims, "harmony_rna", P)
    red <- "harmony_rna"; dims <- seq_along(dims)
  }
  list(obj = obj, red = red, dims = dims)
}

run_harmony <- function(obj, reduction, dims, save, P) {
  args <- list(object = obj, group.by.vars = "sample_id", reduction.use = reduction, dims.use = dims,
               reduction.save = save, project.dim = FALSE, verbose = FALSE)
  if ("ncores" %in% names(formals(harmony:::RunHarmony.default))) args$ncores <- min(P$threads, 8)
  obj <- do.call(harmony::RunHarmony, args)
  obj
}

run_atac <- function(obj, P, multi) {
  Seurat::DefaultAssay(obj) <- "ATAC"
  wb_step("TF-IDF and LSI")
  obj <- Signac::FindTopFeatures(obj, min.cutoff = 5, verbose = FALSE)
  obj <- Signac::RunTFIDF(obj, verbose = FALSE)
  n <- min(P$npcs, ncol(obj) - 1, nrow(obj) - 1)
  obj <- Signac::RunSVD(obj, n = n, verbose = FALSE)
  emb <- SeuratObject::Embeddings(obj, "lsi")
  r <- apply(emb, 2, function(x) stats::cor(x, log1p(obj$nCount_ATAC)))
  write_tsv(data.frame(component = seq_along(r), correlation_with_log_depth = r),
            file.path(P$out, "tables", "lsi_depth_correlations.tsv"))
  dims <- P$dims[P$dims <= n]
  if (abs(r[1]) > 0.5) { dims <- setdiff(dims, 1); say("  LSI component 1 follows depth (r = ", round(r[1], 2), "): left out") }
  else say("  LSI component 1 correlation with depth r = ", round(r[1], 2), ": kept")
  red <- "lsi"
  if (multi && P$integrate == "harmony") {
    wb_step("Harmony by sample on LSI")
    obj <- run_harmony(obj, "lsi", dims, "harmony_atac", P)
    red <- "harmony_atac"; dims <- seq_along(dims)
  }
  list(obj = obj, red = red, dims = dims, depth_cor = r)
}

run_gene_activity <- function(obj, P) {
  wb_step("Gene activity (Signac GeneActivity)")
  ga <- with_workers(min(P$threads, 8), Signac::GeneActivity(obj, assay = "ATAC", verbose = FALSE))
  obj[["ACTIVITY"]] <- SeuratObject::CreateAssayObject(counts = ga)
  obj <- Seurat::NormalizeData(obj, assay = "ACTIVITY", normalization.method = "LogNormalize",
                               scale.factor = stats::median(obj$nCount_ACTIVITY), verbose = FALSE)
  obj
}

# ------------------------------------------------------------------ reference / label transfer

# Counts of a Seurat v3/v4 assay, or of a Seurat v5 Assay5 read without SeuratObject 5 (layers are attributes)
reference_counts <- function(ref) {
  assays <- names(ref@assays)
  assay <- if ("RNA" %in% assays) "RNA" else SeuratObject::DefaultAssay(ref)
  a <- ref@assays[[assay]]
  if (!is.null(attr(a, "layers", exact = TRUE))) {
    layers <- attr(a, "layers", exact = TRUE); fm <- attr(a, "features", exact = TRUE); cm <- attr(a, "cells", exact = TRUE)
    hits <- names(layers)[names(layers) == "counts" | startsWith(names(layers), "counts.")]
    if (!length(hits)) fail("The reference's ", assay, " assay has no counts layer")
    mats <- lapply(hits, function(h) { m <- layers[[h]]
      dimnames(m) <- list(rownames(fm)[as.logical(fm[, h])], rownames(cm)[as.logical(cm[, h])]); methods::as(m, "CsparseMatrix") })
    return(cbind_union(mats))
  }
  m <- methods::slot(a, "counts")
  if (!length(m) || !nrow(m)) fail("The reference's ", assay, " assay has no raw counts")
  m
}

reference_meta <- function(ref) {
  if (methods::is(ref, "Seurat") || "Seurat" %in% class(ref)) return(ref@meta.data)
  if ("SingleCellExperiment" %in% class(ref) || methods::is(ref, "SummarizedExperiment"))
    return(as.data.frame(SummarizedExperiment::colData(ref)))
  if (is.data.frame(ref)) return(ref)
  fail("The reference is not a Seurat or SingleCellExperiment object (class ", class(ref)[1], ")")
}

label_candidates <- function(md) {
  out <- list()
  for (c in names(md)) {
    v <- md[[c]]
    if (!(is.character(v) || is.factor(v))) next
    tab <- sort(table(as.character(v)), decreasing = TRUE)
    if (length(tab) >= 2 && length(tab) <= 300) out[[c]] <- tab
  }
  out
}

suggest_label <- function(cands) {
  if (!length(cands)) return(NA_character_)
  low <- tolower(names(cands))
  for (p in LABEL_PREFERENCE) if (p %in% low) return(names(cands)[match(p, low)])
  hit <- grep("cell.?type|annotation|label", low)
  if (length(hit)) return(names(cands)[hit[1]])
  names(cands)[1]
}

inspect_reference <- function(path) {
  if (!file.exists(path)) fail("Reference not found: ", path)
  suppressPackageStartupMessages(requireNamespace("SeuratObject", quietly = TRUE))
  ref <- suppressMessages(suppressWarnings(readRDS(path)))
  md <- reference_meta(ref)
  cands <- label_candidates(md)
  cols <- lapply(names(cands), function(c) list(name = c, levels = length(cands[[c]]),
                                                 examples = as.list(utils::head(names(cands[[c]]), 5))))
  res <- list(cells = nrow(md), columns = cols, suggested = suggest_label(cands))
  cat(jsonlite::toJSON(res, auto_unbox = TRUE, null = "null", na = "null"), "\n", sep = "")
}

prepare_reference <- function(P, method) {
  info <- file.info(P$reference)
  key <- paste(normalizePath(P$reference), info$size, as.numeric(info$mtime), P$ref_label_req, P$ref_max_cells, method, P$seed)
  cache <- file.path(P$out, "checkpoints", paste0("reference_", method, ".rds"))
  if (file.exists(cache)) {
    r <- readRDS(cache)
    if (identical(r$key, key)) { say("  prepared reference from ", cache); return(r) }
  }
  wb_step("Reading reference ", basename(P$reference))
  ref0 <- readRDS(P$reference)
  if ("SingleCellExperiment" %in% class(ref0)) ref0 <- Seurat::as.Seurat(ref0, counts = "counts", data = NULL)
  md <- reference_meta(ref0)
  cands <- label_candidates(md)
  label <- if (P$ref_label_req == "auto") suggest_label(cands) else P$ref_label_req
  if (is.na(label)) fail("The reference has no label column (character / factor metadata with 2-300 values)")
  if (!label %in% names(md)) fail("Reference has no column '", label, "'; label columns: ", paste(names(cands), collapse = ", "))
  say("  reference label column: ", label)
  counts <- reference_counts(ref0)
  rm(ref0); invisible(gc())
  md <- md[colnames(counts), , drop = FALSE]
  cells <- rownames(md)[!is.na(md[[label]]) & nzchar(as.character(md[[label]]))]
  if (length(cells) > P$ref_max_cells) {             # downsample, keeping up to 200 cells of every label
    set.seed(P$seed); frac <- P$ref_max_cells / length(cells)
    cells <- unlist(lapply(split(cells, as.character(md[cells, label])), function(v)
      if (length(v) <= 200) v else sample(v, max(200, round(length(v) * frac)))), use.names = FALSE)
    say("  reference downsampled to ", fmt(length(cells)), " cells (--ref-max-cells)")
  }
  ref <- SeuratObject::CreateSeuratObject(counts[, cells], meta.data = md[cells, , drop = FALSE], project = "reference")
  ref$wb_label <- as.character(md[cells, label])
  rm(counts); invisible(gc())
  wb_step("Preparing reference (", if (method == "sct") "SCTransform v2 + PCA" else "log-normalization", ")")
  if (method == "sct") {
    ref <- suppressWarnings(Seurat::SCTransform(ref, vst.flavor = "v2", variable.features.n = 3000,
                                                ncells = min(5000, ncol(ref)), seed.use = P$seed, verbose = FALSE))
    ref <- Seurat::RunPCA(ref, npcs = min(50, ncol(ref) - 1), seed.use = P$seed, verbose = FALSE)
  } else {
    ref <- Seurat::NormalizeData(ref, verbose = FALSE)
    ref <- Seurat::FindVariableFeatures(ref, nfeatures = 3000, verbose = FALSE)
  }
  r <- list(key = key, ref = ref, label = label)
  save_rds_atomic(r, cache, compress = FALSE)
  r
}

transfer_labels <- function(obj, R, P, method, weight_red, weight_dims) {
  ref <- R$ref
  wb_step("Label transfer from the reference (", R$label, ")")
  if (method == "sct") {
    Seurat::DefaultAssay(obj) <- "SCT"
    feats <- intersect(SeuratObject::VariableFeatures(ref), rownames(obj[["SCT"]]))
    dims <- seq_len(min(max(P$dims), ncol(SeuratObject::Embeddings(ref, "pca"))))
    say("  SCT transfer anchors on ", length(feats), " shared variable genes, dims 1:", max(dims))
    if (length(feats) < 100) fail("Only ", length(feats), " variable genes of the reference are in the data (same species and gene names?)")
    if (length(feats) < 1000) note("Label transfer used only ", length(feats), " shared variable genes")
    anchors <- Seurat::FindTransferAnchors(reference = ref, query = obj, reference.assay = "SCT", query.assay = "SCT",
      normalization.method = "SCT", reference.reduction = "pca", recompute.residuals = TRUE, features = feats,
      dims = dims, verbose = FALSE)
    wdims <- seq_len(min(max(dims), ncol(SeuratObject::Embeddings(obj, "pca"))))
    wred <- obj[["pca"]]
  } else {
    Seurat::DefaultAssay(obj) <- "ACTIVITY"
    feats <- intersect(SeuratObject::VariableFeatures(ref), rownames(obj[["ACTIVITY"]]))
    say("  CCA transfer anchors (gene activity vs reference RNA) on ", length(feats), " genes")
    if (length(feats) < 100) fail("Only ", length(feats), " variable genes of the reference have gene activities (same species?)")
    dims <- seq_len(min(30, length(feats) - 1, ncol(obj) - 1))
    anchors <- Seurat::FindTransferAnchors(reference = ref, query = obj, reference.assay = "RNA",
      query.assay = "ACTIVITY", features = feats, reduction = "cca", dims = dims, verbose = FALSE)
    wred <- obj[[weight_red]]; wdims <- weight_dims
  }
  n_anchor <- nrow(methods::slot(anchors, "anchors"))
  pred <- Seurat::TransferData(anchorset = anchors, refdata = ref$wb_label, weight.reduction = wred, dims = wdims,
                               k.weight = min(50, max(5, n_anchor - 1)), verbose = FALSE)
  lab <- gsub("[^A-Za-z0-9_.]", "_", R$label)
  score <- pred[colnames(obj), "prediction.score.max"]
  raw <- as.character(pred[colnames(obj), "predicted.id"])
  obj[[paste0("predicted_", lab)]] <- ifelse(score < P$ref_min_score, "Unassigned", raw)
  obj[[paste0("predicted_", lab, "_score")]] <- score
  obj[[paste0("predicted_", lab, "_raw")]] <- raw
  say("  ", fmt(sum(score < P$ref_min_score)), " cells below score ", P$ref_min_score, " are Unassigned")
  attr(obj, "wb_label") <- lab
  list(obj = obj, column = paste0("predicted_", lab), unassigned = sum(score < P$ref_min_score),
       features = length(feats), method = if (method == "sct") "SCT anchors (FindTransferAnchors / TransferData)"
                                          else "CCA anchors: gene activity vs reference RNA")
}

# ------------------------------------------------------------------ figures

theme_wb <- function() ggplot2::theme(text = ggplot2::element_text(family = "Arial", size = 15),
                                      plot.title = ggplot2::element_text(size = 18, face = "bold"))

save_gg <- function(p, file, width, height) {
  draw_figure(file, width, height, function() print(p))
}

umap_plot <- function(obj, red, group, title, label = TRUE) {
  n_groups <- length(unique(obj[[group]][, 1]))
  p <- Seurat::DimPlot(obj, reduction = red, group.by = group, label = label && n_groups <= 60, repel = TRUE,
                       label.size = 5, raster = ncol(obj) > 100000) + ggplot2::labs(title = title) + theme_wb()
  if (n_groups > 40) p <- p + ggplot2::theme(legend.position = "none")
  p
}

# ------------------------------------------------------------------ exports (h5ad / h5mu through Python anndata)

PY_EXPORT <- r"---(
import json, os, sys
import h5py, numpy as np, pandas as pd, scipy.sparse as sp, anndata as ad

def csr(g):
    return sp.csr_matrix((g["data"][:], g["indices"][:], g["indptr"][:]), shape=tuple(int(v) for v in g.attrs["shape"]))

def frame(path, types):
    strcols = {c: str for c, t in types.items() if t["type"] in ("category", "string")}
    df = pd.read_csv(path, index_col=0, dtype=strcols, low_memory=False)
    df.index = df.index.astype(str)
    for c, t in types.items():
        if c not in df.columns:
            continue
        if t["type"] == "category":
            df[c] = pd.Categorical(df[c], categories=[str(v) for v in t["levels"]])
        elif t["type"] == "string":
            df[c] = df[c].astype("category")
        elif t["type"] == "bool" and df[c].dtype != bool:
            df[c] = df[c].astype(str).astype("category")
    return df

def modality(d):
    types = json.load(open(os.path.join(d, "obs_types.json")))
    obs = frame(os.path.join(d, "obs.csv"), types)
    with h5py.File(os.path.join(d, "data.h5"), "r") as f:
        X = csr(f["X"]).astype(np.float32)
        layers = {k: csr(f["layers"][k]).astype(np.float32) for k in f["layers"]} if "layers" in f else {}
        var = pd.DataFrame(index=pd.Index(f["var_names"].asstr()[:], name=None))
        if "highly_variable" in f:
            var["highly_variable"] = f["highly_variable"][:].astype(bool)
        obs_names = list(f["obs_names"].asstr()[:])
        obsm = {}
        if "obsm" in f:
            for k in f["obsm"]:
                a = f["obsm"][k][:]
                obsm[k] = a.reshape(int(np.ravel(f["obsm"][k].attrs["n"])[0]), -1)
    obs = obs.loc[obs_names]
    a = ad.AnnData(X=X, obs=obs, var=var, layers=layers, obsm=obsm)
    a.var_names_make_unique()
    return a

kind, out = sys.argv[1], sys.argv[2]
mods = dict(arg.split("=", 1) for arg in sys.argv[3:])
tmp = out + ".tmp"
if kind == "h5ad":
    a = modality(list(mods.values())[0])
    a.uns["omics_workbench"] = json.dumps(json.load(open(os.path.join(list(mods.values())[0], "uns.json"))))
    a.write_h5ad(tmp, compression="gzip")
    print("h5ad", a.shape)
else:
    import mudata as md
    md.set_options(pull_on_update=False) if hasattr(md, "set_options") else None
    first = list(mods.values())[0]
    m = md.MuData({k: modality(v) for k, v in mods.items()})
    types = json.load(open(os.path.join(first, "obs_types.json")))
    obs = frame(os.path.join(first, "obs.csv"), types).loc[m.obs_names]
    m.obs = obs
    if os.path.exists(os.path.join(first, "global.h5")):
        with h5py.File(os.path.join(first, "global.h5"), "r") as f:
            for k in f["obsm"]:
                m.obsm[k] = f["obsm"][k][:].reshape(int(np.ravel(f["obsm"][k].attrs["n"])[0]), -1)
    m.uns["omics_workbench"] = json.dumps(json.load(open(os.path.join(first, "uns.json"))))
    m.write(tmp)
    print("h5mu", {k: v.shape for k, v in m.mod.items()})
os.replace(tmp, out)
)---"

h5_write_csr <- function(group, m) {
  m <- methods::as(m, "CsparseMatrix"); if (!methods::is(m, "dgCMatrix")) m <- methods::as(m, "dgCMatrix")
  group$create_dataset("data", robj = as.numeric(m@x), dtype = hdf5r::h5types$H5T_NATIVE_FLOAT, gzip_level = 1, chunk_dims = "auto")
  group$create_dataset("indices", robj = m@i, dtype = hdf5r::h5types$H5T_NATIVE_INT32, gzip_level = 1, chunk_dims = "auto")
  group$create_dataset("indptr", robj = as.numeric(m@p), dtype = hdf5r::h5types$H5T_NATIVE_INT64, gzip_level = 1, chunk_dims = "auto")
  hdf5r::h5attr(group, "shape") <- c(ncol(m), nrow(m))       # dgC genes x cells = CSR cells x genes
}

h5_write_emb <- function(group, name, emb) {
  group$create_dataset(name, robj = as.numeric(t(emb)), dtype = hdf5r::h5types$H5T_NATIVE_FLOAT)
  hdf5r::h5attr(group[[name]], "n") <- nrow(emb)
}

write_obs <- function(meta, dir) {
  types <- list()
  for (c in names(meta)) {
    v <- meta[[c]]
    if (is.factor(v)) types[[c]] <- list(type = "category", levels = as.list(levels(v)))
    else if (is.logical(v)) types[[c]] <- list(type = "bool")
    else if (is.character(v)) types[[c]] <- list(type = "string")
  }
  utils::write.csv(meta, file.path(dir, "obs.csv"), row.names = TRUE)
  writeLines(jsonlite::toJSON(types, auto_unbox = TRUE), file.path(dir, "obs_types.json"))
}

write_modality <- function(obj, assay, dir, reductions, uns) {
  dir.create(dir, recursive = TRUE, showWarnings = FALSE)
  f <- hdf5r::H5File$new(file.path(dir, "data.h5"), mode = "w"); on.exit(f$close_all())
  h5_write_csr(f$create_group("X"), SeuratObject::GetAssayData(obj, assay = assay, slot = "data"))
  h5_write_csr(f$create_group("layers")$create_group("counts"), SeuratObject::GetAssayData(obj, assay = assay, slot = "counts"))
  f[["obs_names"]] <- colnames(obj); f[["var_names"]] <- rownames(obj[[assay]])
  hv <- if (assay == "RNA" && "SCT" %in% SeuratObject::Assays(obj)) SeuratObject::VariableFeatures(obj[["SCT"]])
        else SeuratObject::VariableFeatures(obj[[assay]])
  if (length(hv)) f[["highly_variable"]] <- as.integer(rownames(obj[[assay]]) %in% hv)
  g <- f$create_group("obsm")
  for (k in names(reductions)) if (reductions[[k]] %in% SeuratObject::Reductions(obj))
    h5_write_emb(g, k, SeuratObject::Embeddings(obj, reductions[[k]]))
  write_obs(obj[[]], dir)
  writeLines(jsonlite::toJSON(uns, auto_unbox = TRUE, null = "null"), file.path(dir, "uns.json"))
}

python_bin <- function(P) {
  for (p in c(P$python, Sys.getenv("WB_PYTHON"), "/usr/local/bin/python", Sys.which("python3")))
    if (!is.na(p) && nzchar(p) && file.exists(p)) return(p)
  NA_character_
}

check_python <- function(P, modules) {
  py <- python_bin(P)
  if (is.na(py)) return(list(ok = FALSE, msg = "no Python found"))
  r <- suppressWarnings(system2(py, c("-I", "-c", shQuote(paste0("import ", paste(modules, collapse = ", ")))), stdout = TRUE, stderr = TRUE))
  st <- attr(r, "status"); if (is.null(st)) st <- 0
  list(ok = st == 0, py = py, msg = paste(r, collapse = " "))
}

run_python_export <- function(P, kind, out_file, mods) {
  script <- file.path(P$out, "checkpoints", "export_anndata.py")
  writeLines(PY_EXPORT, script)
  args <- c("-I", shQuote(script), kind, shQuote(out_file), vapply(names(mods), function(k) shQuote(paste0(k, "=", mods[[k]])), ""))
  r <- suppressWarnings(system2(P$py, args, stdout = TRUE, stderr = TRUE, env = c("OMP_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1")))
  cat(paste0("  ", r), sep = "\n")
  st <- attr(r, "status")
  if (!is.null(st) && st != 0) fail("Writing ", basename(out_file), " failed (Python exit ", st, ")")
  invisible(TRUE)
}

export_anndata <- function(obj, P, mode, red) {
  outputs <- list()
  uns <- list(script = "sc_analysis.R", version = SCRIPT_VERSION, mode = mode, genome = P$genome)
  stage <- file.path(P$out, "checkpoints", "export")
  unlink(stage, recursive = TRUE); dir.create(stage, recursive = TRUE)
  on.exit(unlink(stage, recursive = TRUE), add = TRUE)
  rna_reds <- list(X_pca = "pca", X_harmony = "harmony_rna", X_umap = if (mode == "multiome") "umap_rna" else "umap")
  atac_reds <- list(X_lsi = "lsi", X_harmony = "harmony_atac", X_umap = if (mode == "multiome") "umap_atac" else "umap")
  if ("h5ad" %in% P$formats) {
    wb_step("Writing h5ad")
    assay <- if (mode == "gex") "RNA" else "ATAC"
    write_modality(obj, assay, file.path(stage, "m"), if (mode == "gex") rna_reds else atac_reds, uns)
    file <- file.path(P$out, paste0(P$name, ".h5ad"))
    run_python_export(P, "h5ad", file, list(m = file.path(stage, "m")))
    outputs$h5ad <- basename(file)
    if (mode == "atac" && "ACTIVITY" %in% SeuratObject::Assays(obj)) note("The h5ad holds the peak matrix; gene activities are in the RDS (assay ACTIVITY)")
  }
  if ("h5mu" %in% P$formats) {
    wb_step("Writing h5mu")
    write_modality(obj, "RNA", file.path(stage, "rna"), rna_reds, uns)
    write_modality(obj, "ATAC", file.path(stage, "atac"), atac_reds, uns)
    g <- hdf5r::H5File$new(file.path(stage, "rna", "global.h5"), mode = "w")
    gg <- g$create_group("obsm"); h5_write_emb(gg, "X_umap", SeuratObject::Embeddings(obj, "umap_wnn")); g$close_all()
    if (P$mudata) {
      file <- file.path(P$out, paste0(P$name, ".h5mu"))
      run_python_export(P, "h5mu", file, list(rna = file.path(stage, "rna"), atac = file.path(stage, "atac")))
      outputs$h5mu <- basename(file)
    } else {
      note("Python mudata is not installed: wrote ", P$name, "_rna.h5ad and ", P$name, "_atac.h5ad instead of an h5mu")
      for (m in c("rna", "atac")) {
        file <- file.path(P$out, paste0(P$name, "_", m, ".h5ad"))
        run_python_export(P, "h5ad", file, stats::setNames(list(file.path(stage, m)), m))
        outputs[[paste0("h5ad_", m)]] <- basename(file)
      }
    }
  }
  outputs
}

# ------------------------------------------------------------------ stage full

run_full <- function(samples, mode, ctx, P, out) {
  ck <- file.path(out, "checkpoints", "qc_object.rds")
  qc <- NULL
  if (file.exists(ck)) {
    wb_step("Loading QC checkpoint")
    qc <- readRDS(ck)
    if (!identical(qc$key, ctx$key)) { say("  checkpoint was made for other inputs or options: recomputing QC"); qc <- NULL }
  }
  if (is.null(qc)) qc <- run_qc(samples, mode, ctx, P, out)
  metrics <- qc$metrics
  wb_progress(32)

  wb_step("Applying QC thresholds")
  if (!is.na(P$thresholds)) {
    th <- read_thresholds(P$thresholds, metrics); th_source <- "user"
    say("  thresholds from ", P$thresholds)
  } else {
    th <- propose_thresholds(metrics, P)$thresholds; th_source <- "proposed"
    say("  no --thresholds: applying the proposed thresholds")
  }
  res <- apply_thresholds(metrics, th)
  th$cells_total <- as.integer(table(factor(metrics$sample_id, th$sample_id)))
  th$cells_pass <- as.integer(tapply(res$pass, factor(metrics$sample_id, th$sample_id), sum))
  th <- th[, c(intersect(threshold_columns("nCount_RNA" %in% names(metrics), "nCount_ATAC" %in% names(metrics)), names(th)))]
  write_tsv(th, file.path(out, "tables", "qc_thresholds_applied.tsv"))
  write_tsv(data.frame(barcode = metrics$barcode, sample_id = metrics$sample_id, qc_pass = res$pass, qc_fail_reasons = res$why),
            file.path(out, "tables", "qc_cell_decisions.tsv.gz"))
  write_tsv(qc_summary_table(metrics, th, res), file.path(out, "tables", "qc_summary.tsv"))
  print(th, row.names = FALSE)
  keep <- rownames(metrics)[res$pass]
  if (!length(keep)) fail("No cells pass the QC thresholds")
  if (length(keep) < 50) fail("Only ", length(keep), " cells pass the QC thresholds (at least 50 are needed)")
  say("  ", fmt(length(keep)), " of ", fmt(nrow(metrics)), " cells pass")

  # label-transfer reference before the long steps: a wrong label column fails early
  method <- if (mode == "atac") "lognorm" else "sct"
  R <- if (!is.na(P$reference)) prepare_reference(P, method) else NULL

  wb_step("Building the filtered object"); wb_progress(36)
  obj <- build_object(qc, keep, mode, P)
  obj$qc_pass <- TRUE
  per_sample_before <- table(metrics$sample_id)
  rm(qc); invisible(gc())
  obj <- run_doublets(obj, P)
  multi <- length(unique(obj$sample_id)) > 1
  if (!multi && P$integrate == "harmony") say("  one sample: no Harmony integration")
  info <- list()

  if (mode %in% c("gex", "multiome")) {
    wb_progress(40)
    r <- run_rna(obj, P, multi); obj <- r$obj; info$rna <- r[c("red", "dims")]
  }
  if (mode %in% c("atac", "multiome")) {
    wb_progress(55)
    a <- run_atac(obj, P, multi); obj <- a$obj; info$atac <- a[c("red", "dims", "depth_cor")]
  }
  wb_progress(62)
  if (mode == "gex") {
    wb_step("Neighbors, clusters and UMAP")
    obj <- Seurat::FindNeighbors(obj, reduction = r$red, dims = r$dims, verbose = FALSE)
    obj <- add_clusters(obj, "SCT_snn", P$resolution, 1)
    obj <- Seurat::RunUMAP(obj, reduction = r$red, dims = r$dims, reduction.name = "umap", reduction.key = "UMAP_",
                           seed.use = P$seed, verbose = FALSE)
    main_umap <- "umap"
  } else if (mode == "atac") {
    wb_step("Neighbors, clusters and UMAP")
    obj <- Seurat::FindNeighbors(obj, reduction = a$red, dims = a$dims, verbose = FALSE)
    obj <- add_clusters(obj, "ATAC_snn", P$resolution, 3)
    obj <- Seurat::RunUMAP(obj, reduction = a$red, dims = a$dims, reduction.name = "umap", reduction.key = "UMAP_",
                           seed.use = P$seed, verbose = FALSE)
    main_umap <- "umap"
  } else {
    wb_step("RNA and ATAC UMAPs")
    obj <- Seurat::RunUMAP(obj, reduction = r$red, dims = r$dims, reduction.name = "umap_rna", reduction.key = "rnaUMAP_",
                           seed.use = P$seed, verbose = FALSE)
    obj <- Seurat::RunUMAP(obj, reduction = a$red, dims = a$dims, reduction.name = "umap_atac", reduction.key = "atacUMAP_",
                           seed.use = P$seed, verbose = FALSE)
    wb_step("Weighted nearest neighbors (WNN), clusters and WNN UMAP")
    obj <- Seurat::FindMultiModalNeighbors(obj, reduction.list = list(r$red, a$red), dims.list = list(r$dims, a$dims),
                                           modality.weight.name = c("RNA.weight", "ATAC.weight"), verbose = FALSE)
    obj <- Seurat::RunUMAP(obj, nn.name = "weighted.nn", reduction.name = "umap_wnn", reduction.key = "wnnUMAP_",
                           seed.use = P$seed, verbose = FALSE)
    obj <- add_clusters(obj, "wsnn", P$resolution, 3)
    main_umap <- "umap_wnn"
  }
  wb_progress(70)
  if (mode %in% c("atac", "multiome") && (P$gene_activity || (mode == "atac" && !is.null(R))))
    obj <- run_gene_activity(obj, P)

  lt <- NULL
  if (!is.null(R)) {
    wb_progress(76)
    lt <- transfer_labels(obj, R, P, method, if (mode == "atac") a$red else NULL, if (mode == "atac") a$dims else NULL)
    obj <- lt$obj; rm(R); invisible(gc())
  }

  wb_step("Cluster markers (FindAllMarkers)"); wb_progress(82)
  marker_assay <- if (mode == "atac") "ACTIVITY" else "RNA"
  if (marker_assay %in% SeuratObject::Assays(obj) && length(unique(obj$cluster)) > 1) {
    Seurat::DefaultAssay(obj) <- marker_assay; SeuratObject::Idents(obj) <- "cluster"
    mk <- with_workers(1, Seurat::FindAllMarkers(obj, assay = marker_assay, only.pos = TRUE, max.cells.per.ident = 500,
                                                 logfc.threshold = 0.25, min.pct = 0.1, random.seed = P$seed, verbose = FALSE))
    if (nrow(mk)) mk <- mk[, c("cluster", "gene", "avg_log2FC", "pct.1", "pct.2", "p_val", "p_val_adj")]
    write_tsv(mk, file.path(out, "tables", "cluster_markers.tsv.gz"))
    say("  ", fmt(nrow(mk)), " marker rows (", marker_assay, ")")
  } else note("No markers: one cluster only, or no gene-level assay")
  Seurat::DefaultAssay(obj) <- if (mode == "atac") "ATAC" else "RNA"

  wb_step("Figures"); wb_progress(86)
  figs <- character()
  fig <- function(p, name, w = 10, h = 8) { f <- file.path("figures", name); save_gg(p, file.path(out, f), w, h); figs <<- c(figs, f) }
  for (rr in P$resolution) {
    col <- paste0("cluster_res", rr)
    fig(umap_plot(obj, main_umap, col, sprintf("Clusters (resolution %s)", rr)), paste0("umap_clusters_res", rr, ".png"))
  }
  fig(umap_plot(obj, main_umap, "sample_id", "Samples", label = FALSE), "umap_samples.png")
  if (!is.null(lt)) {
    fig(umap_plot(obj, main_umap, lt$column, paste("Predicted", sub("^predicted_", "", lt$column))),
        paste0("umap_", lt$column, ".png"), 12, 8)
    fig(Seurat::FeaturePlot(obj, reduction = main_umap, features = paste0(lt$column, "_score"), raster = ncol(obj) > 100000) +
          ggplot2::labs(title = "Label transfer score") + theme_wb(), "umap_prediction_score.png", 9, 8)
  }
  if (mode == "multiome") {
    titles <- c(umap_rna = "RNA UMAP", umap_atac = "ATAC UMAP", umap_wnn = "WNN UMAP")
    p <- patchwork::wrap_plots(lapply(names(titles), function(rd) umap_plot(obj, rd, "cluster", titles[[rd]]) +
           ggplot2::theme(legend.position = "none")), nrow = 1)
    fig(p, "umap_modalities.png", 21, 7.5)
  }
  vf <- intersect(c("nCount_RNA", "nFeature_RNA", "percent.mt", "nCount_ATAC", "nFeature_ATAC", "TSS.enrichment",
                    "nucleosome_signal", "FRiP"), names(obj[[]]))
  ns <- length(unique(obj$sample_id))
  p <- Seurat::VlnPlot(obj, features = vf, group.by = "sample_id", pt.size = 0, ncol = min(4, length(vf)), combine = FALSE)
  p <- patchwork::wrap_plots(lapply(p, function(x) x + theme_wb() + ggplot2::theme(legend.position = "none",
            axis.title.x = ggplot2::element_blank())), ncol = min(4, length(vf)))
  fig(p, "qc_violin_filtered.png", max(14, 1.2 * ns * min(4, length(vf))), 4.8 * ceiling(length(vf) / 4))

  wb_step("Saving RDS"); wb_progress(90)
  obj@misc$omics_workbench <- list(script = "sc_analysis.R", version = SCRIPT_VERSION, date = format(Sys.time()),
    mode = mode, genome = P$genome, samples = lapply(samples, function(s) s[c("id", "type", "outs")]),
    thresholds = th, threshold_source = th_source, reductions = info,
    label_transfer = if (!is.null(lt)) list(reference = P$reference, column = lt$column, min_score = P$ref_min_score,
                                            method = lt$method), notes = notes,
    session = utils::capture.output(utils::sessionInfo()))
  outputs <- list()
  if ("rds" %in% P$formats) {
    f <- file.path(out, paste0(P$name, ".rds")); save_rds_atomic(obj, f); outputs$rds <- basename(f)
  }
  wb_progress(94)
  outputs <- c(outputs, export_anndata(obj, P, mode, main_umap))

  wb_step("Summary")
  after <- table(factor(obj$sample_id, names(per_sample_before)))
  per_sample <- lapply(samples, function(s) list(sample_id = s$id, path = s$outs, type = s$type,
    cells_before = as.integer(per_sample_before[[s$id]]), cells_after = as.integer(after[[s$id]])))
  clusters <- lapply(P$resolution, function(rr) list(resolution = rr, column = paste0("cluster_res", rr),
                                                     n = length(unique(obj[[paste0("cluster_res", rr)]][, 1]))))
  qc_figs <- file.path("figures", paste0("qc_", vapply(samples, `[[`, "", "id"), ".png"))
  figs <- c(qc_figs[file.exists(file.path(out, qc_figs))], figs)
  summ <- list(mode = mode, genome = P$genome, cells_before = nrow(metrics), cells_after = ncol(obj),
               per_sample = per_sample, outputs = outputs, figures = as.list(figs),
               thresholds = "tables/qc_thresholds_applied.tsv", threshold_source = th_source,
               tables = list(markers = if (file.exists(file.path(out, "tables", "cluster_markers.tsv.gz"))) "tables/cluster_markers.tsv.gz",
                             qc_cells = "tables/qc_cell_decisions.tsv.gz", qc_summary = "tables/qc_summary.tsv"),
               clusters = clusters, umap = main_umap, assays = SeuratObject::Assays(obj),
               reductions = SeuratObject::Reductions(obj),
               label_transfer = if (!is.null(lt)) list(reference = P$reference, label = sub("^predicted_", "", lt$column),
                 column = lt$column, score_column = paste0(lt$column, "_score"), min_score = P$ref_min_score,
                 unassigned = lt$unassigned, method = lt$method, shared_features = lt$features),
               notes = as.list(notes), runtime_seconds = round(as.numeric(difftime(Sys.time(), START, units = "secs"))))
  writeLines(jsonlite::toJSON(summ, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null", digits = NA),
             file.path(out, "summary.json"))
  invisible(summ)
}

# ------------------------------------------------------------------ self test

self_test <- function() {
  P0 <- list(floors = DEFAULT_FLOORS, lower_caps = DEFAULT_LOWER_CAPS, upper_caps = DEFAULT_UPPER_CAPS)
  # rows of threshold_derivation.tsv (Amgen 2607/2610): raw bounds -> approved bounds
  cases <- list(c("nFeature_RNA", 605.473274369819, 7458.733497244353, 600, 7500),
                c("nCount_RNA", 820.4250740087576, 29240.621372449, 800, 30000),
                c("nFeature_ATAC", 131.83187497991565, 27333.779401021056, 250, 20000),
                c("nCount_RNA", 1053.5558659969768, 43891.99182005851, 1000, 44000),
                c("nCount_ATAC", 112.85018966827154, 48258.33989226549, 500, 49000),
                c("nFeature_RNA", 148.06966567187754, 18506.595006436535, 200, 10000),
                c("nFeature_RNA", 1073.1365120884782, 7473.582522466817, 1000, 7500),
                c("nCount_RNA", 713.9823115647566, 64168.83925041423, 700, 50000),
                c("nFeature_RNA", 459.1819482922378, 6946.130394540777, 450, 7000),
                c("nFeature_ATAC", 132.34755740609566, 17899.77783525176, 250, 18000))
  for (cs in cases) {
    m <- cs[1]; v <- as.numeric(cs[-1])
    lo <- round_lower(min(max(v[1], P0$floors[[m]]), P0$lower_caps[[m]])); hi <- round_upper(min(v[2], P0$upper_caps[[m]]))
    if (lo != v[3] || hi != v[4]) fail("threshold rule: ", m, " gave ", lo, "/", hi, ", expected ", v[3], "/", v[4])
  }
  # count_bounds end to end on synthetic log-normal values
  set.seed(1); x <- round(10^stats::rnorm(5000, 3.3, 0.18))
  b <- count_bounds(x, "nFeature_RNA", 3, P0$floors, P0$lower_caps, P0$upper_caps)
  z <- log10(x + 1); exp_lo <- round_lower(min(max(10^(stats::median(z) - 3 * stats::mad(z)) - 1, 200), 1000))
  if (b$min != exp_lo) fail("count_bounds lower")
  if (mt_bound(c(1, 1, 1, 2, 1), 3, 5, 20) != 5 || mt_bound(c(1, 2, 3, 2, 1), 3, 5, 20) != 6.45) fail("mt floor")
  if (mt_bound(c(10, 30, 20, 25, 15), 3, 5, 20) != 20) fail("mt cap")
  # exclusive count bounds, inclusive percent.mt, NA = no bound
  met <- data.frame(sample_id = "a", nCount_RNA = c(500, 501, 1000, 999), nFeature_RNA = 300, percent.mt = c(5, 5, 5.01, NA))
  th <- data.frame(sample_id = "a", nCount_RNA_min = 500, nCount_RNA_max = 1000, nFeature_RNA_min = NA, nFeature_RNA_max = NA,
                   percent_mt_max = 5)
  r <- apply_thresholds(met, th)
  if (!identical(r$pass, c(FALSE, TRUE, FALSE, FALSE))) fail("apply_thresholds: ", paste(r$pass, collapse = ","))
  o <- parse_args(c("--mode", "gex", "--dims", "1:30", "--resolution=0.5", "--self-test"))
  if (o$mode != "gex" || o$resolution != "0.5" || o[["self-test"]] != "true" || o$npcs != "50") fail("parse_args")
  if (!identical(parse_dims("1:3"), 1:3) || !identical(parse_dims("4"), 1:4)) fail("parse_dims")
  if (round_upper(20000) != 20000 || round_upper(101) != 110 || round_lower(649.9) != 600) fail("rounding")
  cands <- label_candidates(data.frame(a = rep(c("x", "y"), 5), Cell_Type = rep(letters[1:5], 2), n = 1:10))
  if (suggest_label(cands) != "Cell_Type") fail("suggest_label")
  cat("self-test OK\n")
}

# ------------------------------------------------------------------ main

START <- Sys.time()
P <- NULL

main <- function(args) {
  opt <- parse_args(args)
  if (!is.na(opt$help)) { usage(); return(invisible()) }
  if (!is.na(opt[["self-test"]])) { self_test(); return(invisible()) }
  if (!is.na(opt[["inspect-reference"]])) { inspect_reference(opt[["inspect-reference"]]); return(invisible()) }

  # ---- validate everything before loading the analysis packages
  mode <- opt$mode; stage <- opt$stage
  if (is.na(mode) || !mode %in% c("gex", "atac", "multiome")) fail("--mode must be gex, atac or multiome")
  if (!stage %in% c("qc", "full")) fail("--stage must be qc or full")
  if (is.na(opt$out)) fail("--out is required")
  formats <- unique(trimws(strsplit(opt$formats, ",")[[1]]))
  allowed <- if (mode == "multiome") c("rds", "h5mu") else c("rds", "h5ad")
  if (!length(formats) || !all(formats %in% allowed))
    fail("--formats for --mode ", mode, " takes ", paste(allowed, collapse = " and/or "), " (got ", opt$formats, ")")
  if (!opt$integrate %in% c("harmony", "none")) fail("--integrate must be harmony or none")
  if (!opt$doublets %in% c("none", "scDblFinder")) fail("--doublets must be none or scDblFinder")
  res <- suppressWarnings(as.numeric(strsplit(opt$resolution, ",")[[1]]))
  if (!length(res) || anyNA(res) || any(res <= 0)) fail("--resolution must be numbers like 0.4,0.8")
  if (!is.na(opt$thresholds) && !file.exists(opt$thresholds)) fail("--thresholds file not found: ", opt$thresholds)
  if (!is.na(opt$reference) && !file.exists(opt$reference)) fail("--reference file not found: ", opt$reference)
  if (!is.na(opt$gtf) && !file.exists(opt$gtf)) fail("--gtf file not found: ", opt$gtf)
  P <<- list(out = normalizePath(opt$out, mustWork = FALSE), name = gsub("[^A-Za-z0-9._-]", "_", opt$name),
    nmad = num_opt(opt, "nmad", 0.5), floors = parse_named(opt$floors, DEFAULT_FLOORS, "floors"),
    lower_caps = parse_named(opt[["lower-caps"]], DEFAULT_LOWER_CAPS, "lower-caps"),
    upper_caps = parse_named(opt[["upper-caps"]], DEFAULT_UPPER_CAPS, "upper-caps"),
    tss_min = num_opt(opt, "tss-min"), nucleosome_max = num_opt(opt, "nucleosome-max"), frip_min = num_opt(opt, "frip-min"),
    mt_floor = num_opt(opt, "mt-floor", 0), mt_cap = num_opt(opt, "mt-cap", 0),
    reference = if (is.na(opt$reference)) NA else normalizePath(opt$reference), ref_label_req = opt[["ref-label"]],
    ref_min_score = num_opt(opt, "ref-min-score", 0), ref_max_cells = num_opt(opt, "ref-max-cells", 100),
    integrate = opt$integrate, resolution = res, npcs = as.integer(num_opt(opt, "npcs", 2)), dims = parse_dims(opt$dims),
    formats = formats, threads = as.integer(num_opt(opt, "threads", 1)), doublets = opt$doublets,
    thresholds = if (is.na(opt$thresholds)) NA else normalizePath(opt$thresholds),
    max_cells = if (is.na(opt[["max-cells"]])) NA else as.integer(num_opt(opt, "max-cells", 50)),
    seed = as.integer(num_opt(opt, "seed")), gene_activity = tolower(opt[["gene-activity"]]) %in% c("yes", "true", "1"),
    python = opt$python, genome = opt$genome)
  if (!opt$genome %in% c("auto", "hg38", "hg19", "mm10", "mm39")) fail("--genome must be hg38, mm10, mm39 or auto")
  if (stage == "full" && any(c("h5ad", "h5mu") %in% formats)) {
    chk <- check_python(P, c("h5py", "anndata", "pandas", "scipy"))
    if (!chk$ok) fail("h5ad / h5mu export needs Python with anndata, h5py, pandas, scipy: ", chk$msg)
    P$py <<- chk$py
    P$mudata <<- "h5mu" %in% formats && check_python(P, "mudata")$ok
  }
  for (d in c("", "tables", "figures", "checkpoints")) {
    dir.create(file.path(P$out, d), recursive = TRUE, showWarnings = FALSE)
    if (!dir.exists(file.path(P$out, d))) fail("Cannot create the output folder ", file.path(P$out, d))
  }
  wb_step("Checking inputs"); wb_progress(1)
  say("sc_analysis.R ", SCRIPT_VERSION, ": mode ", mode, ", stage ", stage, ", output ", P$out)
  samples <- read_samples(opt$samples, mode)
  for (s in samples) say("  ", s$id, ": ", s$type, " ", s$outs)

  # ---- packages and threads
  wb_step("Loading R packages")
  need <- c("Seurat", "SeuratObject", "Matrix", "hdf5r", "jsonlite", if (mode != "gex") c("Signac", "GenomicRanges", "rtracklayer"),
            if (stage == "full") c("ggplot2", "patchwork", "future"), if (stage == "full" && opt$integrate == "harmony") "harmony")
  miss <- need[!vapply(need, has_pkg, TRUE)]
  if (length(miss)) fail("Missing R packages: ", paste(miss, collapse = ", "))
  suppressPackageStartupMessages({ library(Seurat); if (mode != "gex") library(Signac) })
  if (has_pkg("RhpcBLASctl")) { RhpcBLASctl::blas_set_num_threads(P$threads); RhpcBLASctl::omp_set_num_threads(P$threads) }
  if (has_pkg("data.table")) data.table::setDTthreads(P$threads)
  if (has_pkg("future")) future::plan(future::sequential)
  set.seed(P$seed)

  # ---- genome, mitochondrial genes, gene annotation
  gs <- vapply(samples, function(s) genome_from_name(h5_genome(s$h5)), "")
  if (length(unique(stats::na.omit(gs))) > 1)
    fail("The samples come from different genome references: ", paste(paste0(vapply(samples, `[[`, "", "id"), " ", gs), collapse = ", "))
  g_h5 <- stats::na.omit(gs)[1]
  genome <- if (opt$genome == "auto") g_h5 else opt$genome
  if (!is.na(g_h5) && opt$genome != "auto" && g_h5 != opt$genome) note("--genome ", opt$genome, " but the matrix says ", g_h5)
  mito <- "^MT-"; has_mito <- FALSE
  if (mode != "atac") {
    genes <- tryCatch(h5_gene_names(samples[[1]]$h5), error = function(e) fail("Cannot read ", samples[[1]]$h5, ": ", conditionMessage(e)))
    n_h <- sum(grepl("^MT-", genes)); n_m <- sum(grepl("^mt-", genes))
    mito <- if (n_m > n_h) "^mt-" else "^MT-"; has_mito <- max(n_h, n_m) > 0
    if (!has_mito) note("No mitochondrial genes (^MT- / ^mt-) found: percent.mt is 0 and not filtered")
    if (is.na(genome)) genome <- if (n_m > n_h) "mm10" else if (n_h > 0) "hg38" else NA
    say("  mitochondrial genes: ", mito, " (", max(n_h, n_m), ")")
  }
  if (is.na(genome)) { if (mode == "gex") genome <- "unknown" else fail("Cannot tell the genome from the matrix: give --genome") }
  P$genome <<- genome
  say("  genome: ", genome)
  ctx <- list(genome = genome, mito_pattern = mito, has_mito = has_mito)
  P$has_mito <<- has_mito
  gtf <- opt$gtf
  if (mode != "gex" && is.na(gtf)) gtf <- invocation_gtf(samples[[1]]$outs)
  ctx$key <- list(version = SCRIPT_VERSION, mode = mode, genome = genome, gtf = gtf, max_cells = P$max_cells, seed = P$seed,
                  samples = lapply(samples, function(s) c(s$id, s$outs, s$type)))
  if (mode != "gex") {
    need_ann <- stage == "qc" || !file.exists(file.path(P$out, "checkpoints", "qc_object.rds"))
    if (!need_ann) {                       # the checkpoint carries the annotation; load it only when QC reruns
      k <- tryCatch(readRDS(file.path(P$out, "checkpoints", "qc_object.rds"))$key, error = function(e) NULL)
      need_ann <- !identical(k, ctx$key)
    }
    if (need_ann) { wb_step("Loading gene annotation"); ctx$annotation <- load_annotation(genome, gtf) }
  }

  if (stage == "qc") {
    run_qc(samples, mode, ctx, P, P$out)
  } else {
    run_full(samples, mode, ctx, P, P$out)
  }
  wb_progress(100)
  wb_step("Finished")
  say("done in ", round(as.numeric(difftime(Sys.time(), START, units = "mins")), 1), " min")
}

# errors: a clear message (and, for unexpected errors, the R call stack) on stderr, a ##WB line, exit status 1
status <- tryCatch({
  withCallingHandlers(main(commandArgs(trailingOnly = TRUE)), error = function(e) {
    if (!inherits(e, "wb_error")) {
      calls <- vapply(sys.calls(), function(x) paste(deparse(x, nlines = 1), collapse = ""), "")
      cat("R call stack:\n", paste0("  ", utils::tail(utils::head(calls, -2), 12), "\n"), sep = "", file = stderr())
    }
  })
  0L
}, error = function(e) {
  msg <- conditionMessage(e)
  if (!inherits(e, "wb_error") && !is.null(conditionCall(e)))
    msg <- paste0(msg, " (in ", paste(deparse(conditionCall(e), nlines = 1), collapse = ""), ")")
  cat("##WB step Failed: ", gsub("\n", " ", substr(msg, 1, 300)), "\n", sep = "")
  cat("ERROR: ", msg, "\n", sep = "", file = stderr())
  1L
})
quit(save = "no", status = status)
