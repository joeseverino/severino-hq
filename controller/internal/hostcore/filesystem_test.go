package hostcore

import (
	"encoding/json"
	"errors"
	"slices"
	"testing"
	"time"
)

// A mount table as a host's /proc/self/mounts prints one inside a sandboxed
// unit: the root filesystem, a boot partition, a btrfs device mounted twice,
// kernel and memory filesystems, a container's overlay, a directory covered
// by a tmpfs, and a name with a space in it.
const table = `/dev/sda2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev,size=402404k,mode=755 0 0
/dev/sda1 /boot xfs rw,relatime 0 0
/dev/sdb1 /srv/data btrfs rw,relatime,subvol=/@data 0 0
/dev/sdb1 /srv/backup btrfs rw,relatime,subvol=/@backup 0 0
overlay /var/lib/docker/overlay2/example/merged overlay rw,lowerdir=/example 0 0
/dev/sda2 /tmp ext4 rw,relatime 0 0
/dev/sdc1 /home ext4 rw,relatime 0 0
tmpfs /home tmpfs ro,size=4k 0 0
/dev/sdd1 /mnt/example\040disk ext4 rw,relatime 0 0
`

func TestEachRealFilesystemIsReadOnceWhereItCanBeReached(t *testing.T) {
	got := Mounts([]byte(table))

	want := []string{"/", "/boot", "/srv/data", "/mnt/example disk"}
	if !slices.Equal(got, want) {
		t.Errorf("Mounts = %q, want %q", got, want)
	}
}

func TestAMountBeneathACoveredOneIsCoveredToo(t *testing.T) {
	got := Mounts([]byte("/dev/sda2 / ext4 rw 0 0\n/dev/sdc1 /home/example ext4 rw 0 0\ntmpfs /home tmpfs ro 0 0\n"))

	if !slices.Equal(got, []string{"/"}) {
		t.Errorf("Mounts = %q, want only the root", got)
	}
}

func TestTheReadingSaysEachFilesystemsSpaceAndNothingElse(t *testing.T) {
	space := map[string][2]uint64{"/": {59 << 20, 29 << 30}, "/boot": {800 << 20, 1 << 30}}
	statfs := func(path string) (free, total uint64, err error) {
		said, ok := space[path]
		if !ok {
			return 0, 0, errors.New("no such filesystem")
		}
		return said[0], said[1], nil
	}
	now := time.Date(2030, 1, 1, 6, 0, 0, 0, time.FixedZone("example", 3600))

	data, err := ReadFilesystems([]byte(table), statfs, now)
	if err != nil {
		t.Fatal(err)
	}

	var said map[string]any
	if err := json.Unmarshal(data, &said); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"read_at": "2030-01-01T05:00:00Z",
		"filesystems": []any{
			map[string]any{"mount": "/", "free_bytes": float64(59 << 20), "total_bytes": float64(29 << 30)},
			map[string]any{"mount": "/boot", "free_bytes": float64(800 << 20), "total_bytes": float64(1 << 30)},
		},
	}
	if got, _ := json.Marshal(said); string(got) != string(must(json.Marshal(want))) {
		t.Errorf("reading = %s, want %s", got, must(json.Marshal(want)))
	}
}

func TestNoFilesystemReadIsNoReading(t *testing.T) {
	refused := func(string) (free, total uint64, err error) { return 0, 0, errors.New("refused") }

	if data, err := ReadFilesystems([]byte(table), refused, time.Now()); err == nil {
		t.Errorf("a table statfs answered nothing for reads as %s", data)
	}
}

func must(data []byte, err error) []byte {
	if err != nil {
		panic(err)
	}
	return data
}
