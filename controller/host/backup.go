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

	"filippo.io/age"
)

// BackupDir is where archives are written unless the unit says otherwise.
const BackupDir = "/srv/severino-hq/backups"

// archivePrefix and archiveSuffix name every archive, so retention removes
// nothing else.
const (
	archivePrefix = "severino-hq-"
	archiveSuffix = ".tar.gz.age"
)

// BackupSettings are what the backup unit decides: where archives go, who can
// read them, and how long they stay on this disk.
type BackupSettings struct {
	Dir string
	// Recipients are post-quantum age recipients, at least one: an archive is
	// only ever written encrypted, and to keys a quantum computer cannot break.
	Recipients []*age.HybridRecipient
	// Retention is how long an archive stays in Dir.
	Retention time.Duration
}

// ReadBackupSettings reads the unit's environment. No recipient, one that is
// not post-quantum, or a retention that is not a whole number of days is
// refused before anything is written.
func ReadBackupSettings(getenv func(string) string) (BackupSettings, error) {
	settings := BackupSettings{Dir: getenv("SEVERINO_BACKUP_DIR")}
	if settings.Dir == "" {
		settings.Dir = BackupDir
	}
	for recipient := range strings.SplitSeq(getenv("SEVERINO_BACKUP_AGE_RECIPIENTS"), ",") {
		if recipient = strings.TrimSpace(recipient); recipient == "" {
			continue
		}
		parsed, err := parseRecipient(recipient)
		if err != nil {
			return BackupSettings{}, err
		}
		settings.Recipients = append(settings.Recipients, parsed)
	}
	if len(settings.Recipients) == 0 {
		return BackupSettings{}, errors.New("SEVERINO_BACKUP_AGE_RECIPIENTS names no recipient, and a backup is only written encrypted: set an age1pq1 key made with age-keygen -pq")
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

// parseRecipient admits only a post-quantum hybrid recipient (ML-KEM-768 with
// X25519), naming any other so the unit's setting can be corrected.
func parseRecipient(recipient string) (*age.HybridRecipient, error) {
	if strings.HasPrefix(recipient, "age1pq1") {
		parsed, err := age.ParseHybridRecipient(recipient)
		if err != nil {
			return nil, fmt.Errorf("SEVERINO_BACKUP_AGE_RECIPIENTS: %w", err)
		}
		return parsed, nil
	}
	if _, err := age.ParseX25519Recipient(recipient); err == nil {
		return nil, fmt.Errorf("SEVERINO_BACKUP_AGE_RECIPIENTS names %s, a classic X25519 recipient: a backup is encrypted only to post-quantum age1pq1 keys, made with age-keygen -pq", recipient)
	}
	return nil, fmt.Errorf("SEVERINO_BACKUP_AGE_RECIPIENTS names %q, which is not a post-quantum age1pq1 recipient", recipient)
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
	return m.backup(ctx, settings, database, map[string]string{"media": at["/media"], "exports": at["/exports"], "seal": m.Seal}, time.Now())
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
	fmt.Fprintln(m.Out, "[1/2] Snapshotting the database with VACUUM INTO.")
	if err := m.snapshot(ctx, database, snapshot); err != nil {
		return err
	}

	name := archivePrefix + now.UTC().Format("20060102T150405Z") + archiveSuffix
	target := filepath.Join(settings.Dir, name)
	file, err := os.CreateTemp(settings.Dir, "."+name+".")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	err = m.writeArchive(file, settings.Recipients, snapshot, dirs)
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

// writeArchive encrypts the tarball to recipients as it is assembled, so no
// plaintext archive touches the disk.
func (m *Machine) writeArchive(file io.Writer, recipients []*age.HybridRecipient, snapshot string, dirs map[string]string) error {
	fmt.Fprintf(m.Out, "[2/2] Assembling the archive, encrypted with age to %d post-quantum recipients.\n", len(recipients))
	to := make([]age.Recipient, len(recipients))
	for i, recipient := range recipients {
		to[i] = recipient
	}
	encrypted, err := age.Encrypt(file, to...)
	if err != nil {
		return fmt.Errorf("the archive could not be encrypted: %w", err)
	}
	err = assemble(encrypted, m.Out, snapshot, dirs)
	if closeErr := encrypted.Close(); err == nil {
		err = closeErr
	}
	return err
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
	for _, name := range []string{"media", "exports", "seal"} {
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
		if !entry.Type().IsRegular() || !strings.HasPrefix(name, archivePrefix) || !strings.HasSuffix(name, archiveSuffix) {
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
