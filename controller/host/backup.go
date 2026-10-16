package host

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// backupSettings are the backup unit's settings that reach the script; every
// other variable of this process stays behind.
var backupSettings = []string{
	"SEVERINO_BACKUP_DIR", "SEVERINO_BACKUP_AGE_RECIPIENTS",
	"SEVERINO_BACKUP_REQUIRE_ENCRYPTION", "SEVERINO_BACKUP_RETENTION_DAYS",
}

// Backup runs the backup script over HQ's own volumes, where docker says
// they are, so a host arranges no path for it to find them.
func (m *Machine) Backup(ctx context.Context, getenv func(string) string) error {
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
	command := m.Command(ctx, filepath.Join(m.Lib, "scripts", "backup.sh"))
	command.Env = append(command.Env,
		"SEVERINO_DATABASE_PATH="+database,
		"SEVERINO_MEDIA_ROOT="+at["/media"],
		"SEVERINO_EXPORTS_ROOT="+at["/exports"],
	)
	for _, name := range backupSettings {
		if value := getenv(name); value != "" {
			command.Env = append(command.Env, name+"="+value)
		}
	}
	command.Stdout, command.Stderr = m.Out, m.Out
	return command.Run()
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
