package host

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"context"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// volumes gives the fake host HQ's volumes with a database holding a proof,
// an upload and an export, and returns the backup directory the settings name.
func (f *fakeHost) volumes(t *testing.T) map[string]string {
	t.Helper()
	volume := func(name string) string { return filepath.Join(f.root, "volumes", "severino-hq_severino_"+name) }
	for _, name := range []string{"data", "media/receipts", "exports"} {
		os.MkdirAll(volume(name), 0o755)
	}
	database := filepath.Join(volume("data"), Database)
	sql := "CREATE TABLE proof (value TEXT NOT NULL); INSERT INTO proof VALUES ('restorable');"
	if out, err := exec.Command("sqlite3", database, sql).CombinedOutput(); err != nil {
		t.Fatalf("sqlite3 is needed for the backup drill: %v %s", err, out)
	}
	os.WriteFile(filepath.Join(volume("media"), "receipts", "receipt.txt"), []byte("receipt-proof\n"), 0o644)
	os.Symlink("receipt.txt", filepath.Join(volume("media"), "receipts", "latest"))
	os.WriteFile(filepath.Join(volume("exports"), "report.txt"), []byte("export-proof\n"), 0o644)
	return map[string]string{"SEVERINO_BACKUP_DIR": filepath.Join(f.root, "backups")}
}

func (f *fakeHost) backup(t *testing.T, settings map[string]string) error {
	t.Helper()
	return f.machine.Backup(context.Background(), func(name string) string { return settings[name] })
}

func archives(t *testing.T, dir string) []string {
	t.Helper()
	found, _ := filepath.Glob(filepath.Join(dir, "*"))
	return found
}

// restore extracts an archive the way docs/Backup.md does, with tar.
func restore(t *testing.T, archive io.Reader) string {
	t.Helper()
	into := t.TempDir()
	extract := exec.Command("tar", "-xzf", "-", "-C", into)
	extract.Stdin = archive
	if out, err := extract.CombinedOutput(); err != nil {
		t.Fatalf("the archive does not extract: %v %s", err, out)
	}
	return into
}

func TestABackupRestoresIntoAFreshDatabase(t *testing.T) {
	f := newFakeHost(t)
	settings := f.volumes(t)

	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}

	found := archives(t, settings["SEVERINO_BACKUP_DIR"])
	if len(found) != 1 || !strings.HasSuffix(found[0], ".tar.gz") {
		t.Fatalf("archives %v", found)
	}
	if info, _ := os.Stat(found[0]); info.Mode().Perm() != 0o600 {
		t.Errorf("the archive is %v, readable beyond root", info.Mode().Perm())
	}
	archive, _ := os.Open(found[0])
	defer archive.Close()
	restored := restore(t, archive)
	value, err := exec.Command("sqlite3", filepath.Join(restored, Database), "SELECT value FROM proof;").Output()
	if err != nil || strings.TrimSpace(string(value)) != "restorable" {
		t.Errorf("the restored database answers %q, %v", value, err)
	}
	for path, want := range map[string]string{
		"media/receipts/receipt.txt": "receipt-proof\n",
		"media/receipts/latest":      "receipt-proof\n",
		"exports/report.txt":         "export-proof\n",
	} {
		if body, _ := os.ReadFile(filepath.Join(restored, path)); string(body) != want {
			t.Errorf("%s restored as %q", path, body)
		}
	}
	if link, _ := os.Readlink(filepath.Join(restored, "media/receipts/latest")); link != "receipt.txt" {
		t.Errorf("a link was archived as %q, not as itself", link)
	}
}

func TestWithRecipientsOnlyAgesOutputReachesTheDisk(t *testing.T) {
	f := newFakeHost(t)
	settings := f.volumes(t)
	settings["SEVERINO_BACKUP_AGE_RECIPIENTS"] = "age1first, age1second"
	settings["SEVERINO_BACKUP_REQUIRE_ENCRYPTION"] = "1"

	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}

	found := archives(t, settings["SEVERINO_BACKUP_DIR"])
	if len(found) != 1 || !strings.HasSuffix(found[0], ".tar.gz.age") {
		t.Fatalf("archives %v", found)
	}
	archive, _ := os.Open(found[0])
	defer archive.Close()
	read := bufio.NewReader(archive)
	header, _ := read.ReadString('\n')
	if header != "age -r age1first -r age1second\n" {
		t.Fatalf("the archive did not go through age for both recipients: %q", header)
	}
	if _, err := os.Stat(filepath.Join(restore(t, read), Database)); err != nil {
		t.Error("the encrypted archive does not hold the database")
	}
}

func TestABackupThatCannotBeWhatTheUnitAsksWritesNothing(t *testing.T) {
	for name, change := range map[string]map[string]string{
		"encryption required, no recipients": {"SEVERINO_BACKUP_REQUIRE_ENCRYPTION": "1"},
		"retention not a number of days":     {"SEVERINO_BACKUP_RETENTION_DAYS": "-1"},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFakeHost(t)
			settings := f.volumes(t)
			for key, value := range change {
				settings[key] = value
			}

			if err := f.backup(t, settings); err == nil {
				t.Fatal("the backup ran")
			}
			if found := archives(t, settings["SEVERINO_BACKUP_DIR"]); len(found) != 0 {
				t.Errorf("wrote %v", found)
			}
		})
	}
}

func TestAFailedEncryptionLeavesNoArchive(t *testing.T) {
	f := newFakeHost(t)
	settings := f.volumes(t)
	settings["SEVERINO_BACKUP_AGE_RECIPIENTS"] = "age1example"
	f.fail = []string{"age "}

	if err := f.backup(t, settings); err == nil {
		t.Fatal("a refused encryption read as a backup")
	}
	if found := archives(t, settings["SEVERINO_BACKUP_DIR"]); len(found) != 0 {
		t.Errorf("left %v", found)
	}
}

func TestNoDatabaseNoBackup(t *testing.T) {
	f := newFakeHost(t)

	err := f.backup(t, map[string]string{"SEVERINO_BACKUP_DIR": filepath.Join(f.root, "backups")})

	if err == nil || !strings.Contains(err.Error(), "no database") {
		t.Fatalf("got %v", err)
	}
}

func TestRetentionRemovesOldArchivesAndNothingElse(t *testing.T) {
	dir := t.TempDir()
	now := time.Now()
	ages := map[string]time.Duration{
		"severino-hq-old.tar.gz":      31 * 24 * time.Hour,
		"severino-hq-old.tar.gz.age":  31 * 24 * time.Hour,
		"severino-hq-new.tar.gz.age":  29 * 24 * time.Hour,
		"someone-elses.tar.gz":        90 * 24 * time.Hour,
		"severino-hq-notes.txt":       90 * 24 * time.Hour,
		".severino-hq-x.tar.gz.12345": 90 * 24 * time.Hour,
	}
	for name, age := range ages {
		path := filepath.Join(dir, name)
		os.WriteFile(path, nil, 0o600)
		os.Chtimes(path, now.Add(-age), now.Add(-age))
	}

	if err := prune(dir, 30*24*time.Hour, now); err != nil {
		t.Fatal(err)
	}

	for name := range ages {
		_, err := os.Stat(filepath.Join(dir, name))
		if removed := err != nil; removed != strings.HasPrefix(name, "severino-hq-old") {
			t.Errorf("%s removed: %v", name, removed)
		}
	}
}

// The archive is a plain gzipped tar: what `tar -xzf` reads, entries rooted
// at the names the restore runbook copies.
func TestTheArchiveIsRootedAtTheRunbooksNames(t *testing.T) {
	f := newFakeHost(t)
	settings := f.volumes(t)
	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}
	archive, _ := os.Open(archives(t, settings["SEVERINO_BACKUP_DIR"])[0])
	defer archive.Close()
	unzipped, err := gzip.NewReader(archive)
	if err != nil {
		t.Fatal(err)
	}
	tops := map[string]bool{}
	for reader := tar.NewReader(unzipped); ; {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		top, _, _ := strings.Cut(header.Name, "/")
		tops[top] = true
	}
	if len(tops) != 3 || !tops[Database] || !tops["media"] || !tops["exports"] {
		t.Errorf("top-level entries %v", tops)
	}
}
