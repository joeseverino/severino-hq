package host

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// BackupDir is where archives are written unless the unit says otherwise.
const BackupDir = "/srv/severino-hq/backups"

// archivePrefix names every archive, so retention removes nothing else.
const archivePrefix = "severino-hq-"

// BackupSettings are what the backup unit decides: where archives go, who can
// read them, and how long they stay on this disk.
type BackupSettings struct {
	Dir string
	// Recipients are age recipients; with none the archive is plain.
	Recipients []string
	// Retention is how long an archive stays in Dir.
	Retention time.Duration
}

// ReadBackupSettings reads the unit's environment. Encryption that is
// required without recipients, or a retention that is not a whole number of
// days, is refused before anything is written.
func ReadBackupSettings(getenv func(string) string) (BackupSettings, error) {
	settings := BackupSettings{Dir: getenv("SEVERINO_BACKUP_DIR")}
	if settings.Dir == "" {
		settings.Dir = BackupDir
	}
	for recipient := range strings.SplitSeq(getenv("SEVERINO_BACKUP_AGE_RECIPIENTS"), ",") {
		if recipient = strings.TrimSpace(recipient); recipient != "" {
			settings.Recipients = append(settings.Recipients, recipient)
		}
	}
	if getenv("SEVERINO_BACKUP_REQUIRE_ENCRYPTION") == "1" && len(settings.Recipients) == 0 {
		return BackupSettings{}, errors.New("backup encryption is required but no age recipients are configured")
	}
	days := getenv("SEVERINO_BACKUP_RETENTION_DAYS")
	if days == "" {
		days = "30"
	}
	count, err := strconv.ParseUint(days, 10, 16)
	if err != nil {
		return BackupSettings{}, errors.New("SEVERINO_BACKUP_RETENTION_DAYS must be a non-negative whole number of days")
	}
	settings.Retention = time.Duration(count) * 24 * time.Hour
	return settings, nil
}

// Backup archives HQ's volumes, where docker says they are, so a host
// arranges no path for it to find them.
func (m *Machine) Backup(ctx context.Context, getenv func(string) string) error {
	settings, err := ReadBackupSettings(getenv)
	if err != nil {
		return err
	}
	at := map[string]string{}
	for _, volume := range Volumes {
		mountpoint, err := m.mountpoint(ctx, volume)
		if err != nil {
			return err
		}
		at[volume.Target] = mountpoint
	}
	database := filepath.Join(at["/data"], Database)
	if _, err := os.Stat(database); err != nil {
		return fmt.Errorf("there is no database at %s to back up", database)
	}
	return m.backup(ctx, settings, database, map[string]string{"media": at["/media"], "exports": at["/exports"]}, time.Now())
}

// backup snapshots database, archives it with each directory under its name,
// and writes the archive into settings.Dir whole or not at all.
func (m *Machine) backup(ctx context.Context, settings BackupSettings, database string, dirs map[string]string, now time.Time) error {
	if err := os.MkdirAll(settings.Dir, 0o700); err != nil {
		return err
	}
	stage, err := os.MkdirTemp("", "severino-backup-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	snapshot := filepath.Join(stage, Database)
	fmt.Fprintln(m.Out, "[1/3] Snapshotting the database with VACUUM INTO.")
	if err := m.snapshot(ctx, database, snapshot); err != nil {
		return err
	}

	name := archivePrefix + now.UTC().Format("20060102T150405Z") + ".tar.gz"
	if len(settings.Recipients) > 0 {
		name += ".age"
	}
	target := filepath.Join(settings.Dir, name)
	file, err := os.CreateTemp(settings.Dir, "."+name+".")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	err = m.writeArchive(ctx, file, settings.Recipients, snapshot, dirs)
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(file.Name(), target)
	}
	if err != nil {
		return err
	}
	fmt.Fprintln(m.Out, "Wrote", target)
	return prune(settings.Dir, settings.Retention, now)
}

// snapshot copies the live database consistently and checks the copy.
func (m *Machine) snapshot(ctx context.Context, database, snapshot string) error {
	vacuum := "VACUUM INTO '" + strings.ReplaceAll(snapshot, "'", "''") + "';"
	if out, err := m.Command(ctx, "sqlite3", database, vacuum).CombinedOutput(); err != nil {
		return fmt.Errorf("the database could not be snapshotted: %w: %s", err, out)
	}
	out, err := m.Command(ctx, "sqlite3", snapshot, "PRAGMA integrity_check;").Output()
	if result := strings.TrimSpace(string(out)); err != nil || result != "ok" {
		if err != nil {
			return fmt.Errorf("the snapshot failed its integrity check: %w %s", err, result)
		}
		return fmt.Errorf("the snapshot failed its integrity check: %s", result)
	}
	return nil
}

// writeArchive writes the tarball to file, through age when there are
// recipients, so no plaintext archive touches the disk.
func (m *Machine) writeArchive(ctx context.Context, file *os.File, recipients []string, snapshot string, dirs map[string]string) error {
	fmt.Fprintln(m.Out, "[2/3] Assembling the archive.")
	if len(recipients) == 0 {
		fmt.Fprintln(m.Out, "[3/3] Encryption skipped: SEVERINO_BACKUP_AGE_RECIPIENTS is not set.")
		return assemble(file, m.Out, snapshot, dirs)
	}
	fmt.Fprintln(m.Out, "[3/3] Encrypting it with age.")
	var args []string
	for _, recipient := range recipients {
		args = append(args, "-r", recipient)
	}
	encrypt := m.Command(ctx, "age", args...)
	var refusal strings.Builder
	encrypt.Stdout, encrypt.Stderr = file, &refusal
	plain, err := encrypt.StdinPipe()
	if err != nil {
		return err
	}
	if err := encrypt.Start(); err != nil {
		return fmt.Errorf("age could not be started: %w", err)
	}
	assembled := assemble(plain, m.Out, snapshot, dirs)
	plain.Close()
	if err := encrypt.Wait(); err != nil {
		return fmt.Errorf("age refused the archive: %w: %s", err, refusal.String())
	}
	return assembled
}

// assemble writes a gzipped tar of the snapshot at its top level and each
// directory that exists under its name, warning on warn about a file that
// changed while it was read.
func assemble(w, warn io.Writer, snapshot string, dirs map[string]string) error {
	return (&assembly{warn: warn}).write(w, snapshot, dirs)
}

type assembly struct {
	archive *tar.Writer
	warn    io.Writer
	// opened runs between a file's stat and its copy: a test changes the file
	// there, as HQ writing to it during a backup would.
	opened func(path string)
}

func (a *assembly) write(w io.Writer, snapshot string, dirs map[string]string) error {
	compressed := gzip.NewWriter(w)
	a.archive = tar.NewWriter(compressed)
	err := a.addFile(snapshot, Database)
	for _, name := range []string{"media", "exports"} {
		if dir := dirs[name]; err == nil && dir != "" {
			err = a.addTree(dir, name)
		}
	}
	if closeErr := a.archive.Close(); err == nil {
		err = closeErr
	}
	if closeErr := compressed.Close(); err == nil {
		err = closeErr
	}
	return err
}

func (a *assembly) addFile(path, name string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	return a.addEntry(path, name, info)
}

// addTree adds dir as name, keeping links as links; a directory that is not
// there is left out, as a host with no uploads has none.
func (a *assembly) addTree(dir, name string) error {
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		return nil
	}
	return filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		return a.addEntry(path, filepath.ToSlash(filepath.Join(name, relative)), info)
	})
}

func (a *assembly) addEntry(path, name string, info fs.FileInfo) error {
	if info.Mode().IsRegular() {
		return a.addRegular(path, name)
	}
	link := ""
	if info.Mode()&fs.ModeSymlink != 0 {
		var err error
		if link, err = os.Readlink(path); err != nil {
			return err
		}
	}
	header, err := tar.FileInfoHeader(info, link)
	if err != nil {
		return err
	}
	header.Name = name
	if info.IsDir() {
		header.Name += "/"
	}
	return a.archive.WriteHeader(header)
}

// addRegular archives a file as it stood when it was opened, since HQ may be
// writing it: the header takes the open file's size and exactly that many
// bytes follow it. Bytes a file gained are left out, bytes it lost are zeros,
// and either way the file is named on warn, as GNU tar does.
func (a *assembly) addRegular(path, name string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		fmt.Fprintf(a.warn, "%s: no longer a regular file; left out\n", name)
		return nil
	}
	header, err := tar.FileInfoHeader(info, "")
	if err != nil {
		return err
	}
	header.Name = name
	if a.opened != nil {
		a.opened(path)
	}
	if err := a.archive.WriteHeader(header); err != nil {
		return err
	}
	copied, err := io.CopyN(a.archive, file, header.Size)
	if errors.Is(err, io.EOF) {
		fmt.Fprintf(a.warn, "%s: file shrank by %d bytes; padding with zeros\n", name, header.Size-copied)
		_, err = io.CopyN(a.archive, zeros{}, header.Size-copied)
		return err
	}
	if err != nil {
		return err
	}
	if after, err := file.Stat(); err == nil && (after.Size() != info.Size() || !after.ModTime().Equal(info.ModTime())) {
		fmt.Fprintf(a.warn, "%s: file changed as we read it\n", name)
	}
	return nil
}

// zeros pads an entry whose file shrank to the size its header promised.
type zeros struct{}

func (zeros) Read(p []byte) (int, error) {
	clear(p)
	return len(p), nil
}

// prune removes archives older than retention from dir, and nothing else:
// off-host retention belongs to the replication.
func prune(dir string, retention time.Duration, now time.Time) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		name := entry.Name()
		if !entry.Type().IsRegular() || !strings.HasPrefix(name, archivePrefix) ||
			!strings.HasSuffix(name, ".tar.gz") && !strings.HasSuffix(name, ".tar.gz.age") {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if now.Sub(info.ModTime()) > retention {
			if err := os.Remove(filepath.Join(dir, name)); err != nil {
				return err
			}
		}
	}
	return nil
}

// mountpoint is where docker keeps a volume on this host.
func (m *Machine) mountpoint(ctx context.Context, volume Volume) (string, error) {
	out, err := m.Command(ctx, "docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume.Name).Output()
	if err != nil {
		return "", fmt.Errorf("docker knows no volume %s", volume.Name)
	}
	return strings.TrimSpace(string(out)), nil
}

// schemaSnapshot is where a boot keeps the database before migrating it.
func (m *Machine) schemaSnapshot(ctx context.Context) (string, error) {
	for _, volume := range Volumes {
		if volume.Target == "/data" {
			data, err := m.mountpoint(ctx, volume)
			return filepath.Join(data, SchemaSnapshot), err
		}
	}
	return "", errors.New("HQ declares no data volume")
}
