package host

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"io"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"filippo.io/age"
)

// sealProof is the sealing seed the fake host's controller holds.
const sealProof = "0a1b2c3d.seed"

// volumes gives the fake host HQ's volumes with a database holding a proof,
// an upload and an export, and its controller a sealing seed, and returns settings that name the backup
// directory and one freshly generated post-quantum recipient, with the
// identity that reads what is encrypted to it.
func (f *fakeHost) volumes(t *testing.T) (map[string]string, *age.HybridIdentity) {
	t.Helper()
	volume := func(name string) string { return filepath.Join(f.root, "volumes", "severino-hq_severino_"+name) }
	for _, name := range []string{"data", "media/receipts", "exports"} {
		if err := os.MkdirAll(volume(name), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	database := filepath.Join(volume("data"), Database)
	sql := "CREATE TABLE proof (value TEXT NOT NULL); INSERT INTO proof VALUES ('restorable');"
	if out, err := exec.CommandContext(t.Context(), "sqlite3", database, sql).CombinedOutput(); err != nil {
		t.Fatalf("sqlite3 is needed for the backup drill: %v %s", err, out)
	}
	if err := os.WriteFile(filepath.Join(volume("media"), "receipts", "receipt.txt"), []byte("receipt-proof\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("receipt.txt", filepath.Join(volume("media"), "receipts", "latest")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(volume("exports"), "report.txt"), []byte("export-proof\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(f.machine.Seal, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.machine.Seal, sealProof), []byte("seed-proof"), 0o400); err != nil {
		t.Fatal(err)
	}
	identity := hybridIdentity(t)
	return map[string]string{
		"SEVERINO_BACKUP_DIR":            filepath.Join(f.root, "backups"),
		"SEVERINO_BACKUP_AGE_RECIPIENTS": identity.Recipient().String(),
	}, identity
}

func hybridIdentity(t *testing.T) *age.HybridIdentity {
	t.Helper()
	identity, err := age.GenerateHybridIdentity()
	if err != nil {
		t.Fatal(err)
	}
	return identity
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

// decrypt reads the one archive in dir with identity, as `age -d -i` does.
func decrypt(t *testing.T, dir string, identity age.Identity) io.Reader {
	t.Helper()
	found := archives(t, dir)
	if len(found) != 1 || !strings.HasSuffix(found[0], ".tar.gz.age") {
		t.Fatalf("archives %v", found)
	}
	sealed, err := os.ReadFile(found[0])
	if err != nil {
		t.Fatal(err)
	}
	plain, err := age.Decrypt(bytes.NewReader(sealed), identity)
	if err != nil {
		t.Fatalf("the archive does not decrypt: %v", err)
	}
	return plain
}

// restore extracts an archive the way docs/Backup.md does, with tar.
func restore(t *testing.T, archive io.Reader) string {
	t.Helper()
	into := t.TempDir()
	extract := exec.CommandContext(t.Context(), "tar", "-xzf", "-", "-C", into)
	extract.Stdin = archive
	if out, err := extract.CombinedOutput(); err != nil {
		t.Fatalf("the archive does not extract: %v %s", err, out)
	}
	return into
}

// The restore drill: what the backup wrote decrypts with the recipient's
// identity and extracts into a database that answers, beside the files.
func TestABackupRestoresIntoAFreshDatabase(t *testing.T) {
	f := newFakeHost(t)
	settings, identity := f.volumes(t)

	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}

	dir := settings["SEVERINO_BACKUP_DIR"]
	if info, _ := os.Stat(archives(t, dir)[0]); info.Mode().Perm() != 0o600 {
		t.Errorf("the archive is %v, readable beyond root", info.Mode().Perm())
	}
	restored := restore(t, decrypt(t, dir, identity))
	value, err := exec.CommandContext(t.Context(), "sqlite3", filepath.Join(restored, Database), "SELECT value FROM proof;").Output()
	if err != nil || strings.TrimSpace(string(value)) != "restorable" {
		t.Errorf("the restored database answers %q, %v", value, err)
	}
	for path, want := range map[string]string{
		"media/receipts/receipt.txt": "receipt-proof\n",
		"media/receipts/latest":      "receipt-proof\n",
		"exports/report.txt":         "export-proof\n",
		"seal/" + sealProof:          "seed-proof",
	} {
		if body, _ := os.ReadFile(filepath.Join(restored, path)); string(body) != want {
			t.Errorf("%s restored as %q", path, body)
		}
	}
	if info, err := os.Stat(filepath.Join(restored, "seal", sealProof)); err != nil || info.Mode().Perm() != 0o400 {
		t.Errorf("the sealing seed restored as %v, %v", info, err)
	}
	if link, _ := os.Readlink(filepath.Join(restored, "media", "receipts", "latest")); link != "receipt.txt" {
		t.Errorf("a link was archived as %q, not as itself", link)
	}
}

func TestEveryRecipientCanReadTheArchiveAndNoPlaintextReachesTheDisk(t *testing.T) {
	f := newFakeHost(t)
	settings, first := f.volumes(t)
	second := hybridIdentity(t)
	settings["SEVERINO_BACKUP_AGE_RECIPIENTS"] = first.Recipient().String() + ", " + second.Recipient().String()

	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}

	dir := settings["SEVERINO_BACKUP_DIR"]
	sealed, _ := os.ReadFile(archives(t, dir)[0])
	if !bytes.HasPrefix(sealed, []byte("age-encryption.org/v1\n")) {
		t.Errorf("the archive on disk is not an age file: %q", sealed[:min(len(sealed), 32)])
	}
	for _, identity := range []*age.HybridIdentity{first, second} {
		if _, err := os.Stat(filepath.Join(restore(t, decrypt(t, dir, identity)), Database)); err != nil {
			t.Error("the decrypted archive does not hold the database")
		}
	}
	if _, err := age.Decrypt(bytes.NewReader(sealed), hybridIdentity(t)); err == nil {
		t.Error("an identity the unit does not name read the archive")
	}
}

// A setting the backup cannot honor is refused, naming what is wrong, before
// the database is touched or a file is written.
func TestABackupThatCannotBeWhatTheUnitAsksWritesNothing(t *testing.T) {
	classic, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatal(err)
	}
	hybrid := hybridIdentity(t).Recipient().String()
	for name, refusal := range map[string]struct {
		change map[string]string
		names  string
	}{
		"no recipient":            {map[string]string{"SEVERINO_BACKUP_AGE_RECIPIENTS": " , "}, "SEVERINO_BACKUP_AGE_RECIPIENTS"},
		"a classic recipient":     {map[string]string{"SEVERINO_BACKUP_AGE_RECIPIENTS": classic.Recipient().String()}, classic.Recipient().String()},
		"a classic beside one pq": {map[string]string{"SEVERINO_BACKUP_AGE_RECIPIENTS": hybrid + "," + classic.Recipient().String()}, classic.Recipient().String()},
		"not a recipient":         {map[string]string{"SEVERINO_BACKUP_AGE_RECIPIENTS": "ssh-ed25519 AAAAexample"}, "ssh-ed25519 AAAAexample"},
		"retention not days":      {map[string]string{"SEVERINO_BACKUP_RETENTION_DAYS": "-1"}, "SEVERINO_BACKUP_RETENTION_DAYS"},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFakeHost(t)
			settings, _ := f.volumes(t)
			maps.Copy(settings, refusal.change)

			err := f.backup(t, settings)
			if err == nil {
				t.Fatal("the backup ran")
			}
			if !strings.Contains(err.Error(), refusal.names) {
				t.Errorf("the refusal does not name %s: %v", refusal.names, err)
			}
			if _, statErr := os.Stat(settings["SEVERINO_BACKUP_DIR"]); statErr == nil {
				t.Errorf("wrote %v", archives(t, settings["SEVERINO_BACKUP_DIR"]))
			}
			for _, call := range f.log {
				if strings.HasPrefix(call, "sqlite3 ") {
					t.Errorf("the database was snapshotted before the refusal: %s", call)
				}
			}
		})
	}
}

// An archive that cannot be assembled whole leaves nothing behind.
func TestAFailedArchiveLeavesNoArchive(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("run as root, every file is readable")
	}
	f := newFakeHost(t)
	settings, _ := f.volumes(t)
	unreadable := filepath.Join(f.root, "volumes", "severino-hq_severino_exports", "report.txt")
	if err := os.Chmod(unreadable, 0); err != nil {
		t.Fatal(err)
	}

	if err := f.backup(t, settings); err == nil {
		t.Fatal("an archive missing a file read as a backup")
	}
	if found := archives(t, settings["SEVERINO_BACKUP_DIR"]); len(found) != 0 {
		t.Errorf("left %v", found)
	}
}

func TestNoDatabaseNoBackup(t *testing.T) {
	f := newFakeHost(t)

	err := f.backup(t, map[string]string{
		"SEVERINO_BACKUP_DIR":            filepath.Join(f.root, "backups"),
		"SEVERINO_BACKUP_AGE_RECIPIENTS": hybridIdentity(t).Recipient().String(),
	})

	if err == nil || !strings.Contains(err.Error(), "no database") {
		t.Fatalf("got %v", err)
	}
}

func TestRetentionRemovesOldArchivesAndNothingElse(t *testing.T) {
	dir := t.TempDir()
	now := time.Now()
	ages := map[string]time.Duration{
		"severino-hq-old.tar.gz.age":      31 * 24 * time.Hour,
		"severino-hq-new.tar.gz.age":      29 * 24 * time.Hour,
		"someone-elses.tar.gz.age":        90 * 24 * time.Hour,
		"severino-hq-notes.txt":           90 * 24 * time.Hour,
		".severino-hq-x.tar.gz.age.12345": 90 * 24 * time.Hour,
	}
	for name, age := range ages {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, nil, 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(path, now.Add(-age), now.Add(-age)); err != nil {
			t.Fatal(err)
		}
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

// Decrypted, the archive is a gzipped tar: what `tar -xzf` reads, entries
// rooted at the names the restore runbook copies.
func TestTheArchiveIsRootedAtTheRunbooksNames(t *testing.T) {
	f := newFakeHost(t)
	settings, identity := f.volumes(t)
	if err := f.backup(t, settings); err != nil {
		t.Fatal(err)
	}
	unzipped, err := gzip.NewReader(decrypt(t, settings["SEVERINO_BACKUP_DIR"], identity))
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
	if len(tops) != 4 || !tops[Database] || !tops["media"] || !tops["exports"] || !tops["seal"] {
		t.Errorf("top-level entries %v", tops)
	}
}

func TestAHostWithNoSealingKeyStillBacksUp(t *testing.T) {
	f := newFakeHost(t)
	settings, identity := f.volumes(t)
	if err := os.RemoveAll(f.machine.Seal); err != nil {
		t.Fatal(err)
	}
	if err := f.backup(t, settings); err != nil {
		t.Fatalf("a host with no sealing key yet fails its backup: %v", err)
	}
	restored := restore(t, decrypt(t, settings["SEVERINO_BACKUP_DIR"], identity))
	if _, err := os.Stat(filepath.Join(restored, Database)); err != nil {
		t.Error("the database was not archived")
	}
}

// archiveWhileWriting archives a tree whose upload changes, through change,
// once the backup has opened it, and returns the restored tree and the
// warnings the backup gave.
func archiveWhileWriting(t *testing.T, change func(path string)) (string, string) {
	t.Helper()
	tree := t.TempDir()
	for _, dir := range []string{"media", "exports"} {
		if err := os.Mkdir(filepath.Join(tree, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(tree, Database), []byte("database"), 0o644); err != nil {
		t.Fatal(err)
	}
	upload := filepath.Join(tree, "media", "upload.bin")
	if err := os.WriteFile(upload, []byte("0123456789"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tree, "exports", "report.txt"), []byte("export-proof\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var archive, warnings bytes.Buffer
	building := &assembly{warn: &warnings, opened: func(path string) {
		if path == upload && change != nil {
			change(path)
		}
	}}
	dirs := map[string]string{"media": filepath.Join(tree, "media"), "exports": filepath.Join(tree, "exports")}
	if err := building.write(&archive, filepath.Join(tree, Database), dirs); err != nil {
		t.Fatalf("the backup failed because a file changed under it: %v", err)
	}
	restored := restore(t, &archive)
	if body, _ := os.ReadFile(filepath.Join(restored, "exports", "report.txt")); string(body) != "export-proof\n" {
		t.Errorf("the entry after the changed file restored as %q", body)
	}
	return restored, warnings.String()
}

func TestAFileThatGrowsDuringTheBackupIsArchivedAsItStoodAtOpen(t *testing.T) {
	restored, warnings := archiveWhileWriting(t, func(path string) {
		grown, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
		_, _ = grown.WriteString("appended")
		grown.Close()
	})
	if body, _ := os.ReadFile(filepath.Join(restored, "media", "upload.bin")); string(body) != "0123456789" {
		t.Errorf("the grown file restored as %q", body)
	}
	if !strings.Contains(warnings, "media/upload.bin") {
		t.Errorf("no warning names the file that changed: %q", warnings)
	}
}

func TestAFileThatShrinksDuringTheBackupKeepsItsPlaceAndIsNamed(t *testing.T) {
	restored, warnings := archiveWhileWriting(t, func(path string) {
		if err := os.Truncate(path, 4); err != nil {
			t.Fatal(err)
		}
	})
	body, _ := os.ReadFile(filepath.Join(restored, "media", "upload.bin"))
	if len(body) != 10 || string(body[:4]) != "0123" {
		t.Errorf("the shrunk file restored as %q", body)
	}
	if !strings.Contains(warnings, "media/upload.bin") {
		t.Errorf("no warning names the file that changed: %q", warnings)
	}
}

func TestAnUntouchedTreeIsArchivedWithoutWarnings(t *testing.T) {
	restored, warnings := archiveWhileWriting(t, nil)
	if body, _ := os.ReadFile(filepath.Join(restored, "media", "upload.bin")); string(body) != "0123456789" {
		t.Errorf("the file restored as %q", body)
	}
	if warnings != "" {
		t.Errorf("an unchanged tree warned: %q", warnings)
	}
}
