package host

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheBackupReadsHQsVolumesWhereDockerSays(t *testing.T) {
	f := newFakeHost(t)
	data := filepath.Join(f.root, "volumes", "severino-hq_severino_data")
	os.MkdirAll(data, 0o755)
	os.WriteFile(filepath.Join(data, "severino.sqlite3"), nil, 0o644)
	settings := map[string]string{
		"SEVERINO_BACKUP_AGE_RECIPIENTS": "age1example",
		"DJANGO_SECRET_KEY":              "not-the-backups",
	}

	if err := f.machine.Backup(context.Background(), func(name string) string { return settings[name] }); err != nil {
		t.Fatal(err)
	}

	env, _ := os.ReadFile(filepath.Join(f.root, "backup-env"))
	for _, want := range []string{
		"SEVERINO_DATABASE_PATH=" + filepath.Join(data, "severino.sqlite3"),
		"SEVERINO_MEDIA_ROOT=" + filepath.Join(f.root, "volumes", "severino-hq_severino_media"),
		"SEVERINO_EXPORTS_ROOT=" + filepath.Join(f.root, "volumes", "severino-hq_severino_exports"),
		"SEVERINO_BACKUP_AGE_RECIPIENTS=age1example",
	} {
		if !strings.Contains(string(env), want+"\n") {
			t.Errorf("the script was not given %s:\n%s", want, env)
		}
	}
	if strings.Contains(string(env), "not-the-backups") {
		t.Error("a setting that is not the backup's reached the script")
	}
}

func TestNoDatabaseNoBackup(t *testing.T) {
	f := newFakeHost(t)

	err := f.machine.Backup(context.Background(), func(string) string { return "" })

	if err == nil || !strings.Contains(err.Error(), "no database") {
		t.Fatalf("got %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(f.root, "backup-env")); statErr == nil {
		t.Error("the script ran with no database")
	}
}
