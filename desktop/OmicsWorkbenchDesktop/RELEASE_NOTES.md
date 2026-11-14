Native Omics Workbench builds for Apple Silicon Macs. First launch: see [../../RELEASE_NOTES.md](../../RELEASE_NOTES.md).

## What's new in 1.0.1

- **Type**: the window's text and headings are set in the system font (San Francisco), as the web pages now are
- The pages inside the window follow the app's light / dark setting (checked against a test server in the macOS preview build)

## What's new in 1.0.0

- **Omics Workbench**, the analysis part of Omics Studio, in its own window: the server's `/workbench/` pages with a
  native sidebar — **Overview** (recent jobs, server load), **Download data** (FASTQ), **Bulk sequencing** (RNA-seq,
  ChIP-seq / CUT&RUN / CUT&Tag, ATAC-seq), **Single-cell** (Cell Ranger, Seurat), **Jobs** (each job's log) and, for
  administrators, **Servers** (the server load dashboard). ⌘1 … ⌘6 open the same pages
- **Recent jobs** in the sidebar: what is queued or running first, with its current step; click a job for its log
- **Notifications** when one of your jobs finishes or fails (click one to open the job's log); the app checks every
  30 seconds and keeps doing so with its window closed — click the Dock icon to bring it back. **File → Notify When
  Jobs End** turns them off. The **Dock badge** shows how many of your jobs are running
- **Sign-in** as in Omics Canvas: the server's sign-in page opens in the window (**Keep me signed in** keeps the app
  signed in); **Sign Out** in the application menu
- **Server…** (⌘,): the server address and port (default THOR, 171.65.68.140 port 8892), optionally through
  **SSH port forwarding** (password kept in the macOS Keychain, or your SSH key), as in Omics Canvas
- Links to Genome Canvas open in Genome Canvas.app when it is installed; the viewers and other pages open in the
  web browser
