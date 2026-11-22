// T-3571: embeds the size and mtime of the Python sources sampo-fast mirrors. At run time any mismatch (an edit, a
// merge, another checkout) hands the call to Python until the binary is rebuilt.
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::{env, fs};

// every plugin file the Python hook loads on a call sampo-fast decides (test_fast_equiv checks it against the hook's
// own module dump), and the guard
const WATCHED: &[&str] = &["hooks/hook", "lib/sampo/__init__.py", "lib/sampo/py.py", "lib/sampo/hooks.py",
    "lib/sampo/core.py", "lib/sampo/budget.py", "lib/sampo/spawn.py", "lib/fmguard.py"];

fn main() {
    let plugin = Path::new(&env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let mut out = format!("pub const WATCHED: [(&str, u64, i64, i64); {}] = [\n", WATCHED.len());
    for rel in WATCHED {
        let path = plugin.join(rel);
        println!("cargo:rerun-if-changed={}", path.display());
        let m = fs::metadata(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        out += &format!("    ({rel:?}, {}, {}, {}),\n", m.size(), m.mtime(), m.mtime_nsec());
    }
    fs::write(Path::new(&env::var("OUT_DIR").unwrap()).join("stamps.rs"), out + "];\n").unwrap();
}
