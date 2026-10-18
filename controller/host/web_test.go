package host

import (
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func TestTheContainerRunsUnprivilegedAndIsTheUnitsToRestart(t *testing.T) {
	args := Web{Image: "ghcr.io/example/hq@" + digest}.Args()
	joined := strings.Join(args, " ")

	for _, want := range []string{
		"run --rm --name " + Container,
		"--network host",
		"--cap-drop ALL",
		"--security-opt no-new-privileges:true",
		"--read-only",
		"--pids-limit 512",
		"--memory 1g",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("args lack %q: %s", want, joined)
		}
	}
	// The image carries its collected assets; nothing may cover them.
	if strings.Contains(joined, "target=/static") {
		t.Errorf("a mount covers the image's assets: %s", joined)
	}
	if strings.Contains(joined, "--restart") {
		t.Errorf("the unit restarts HQ; the container must have no restart policy: %s", joined)
	}
	if args[len(args)-1] != "ghcr.io/example/hq@"+digest {
		t.Errorf("the image is not the last argument: %s", joined)
	}
}

func TestEveryVolumeIsMountedAndNoBindIsCreated(t *testing.T) {
	args := Web{Image: "ghcr.io/example/hq@" + digest}.Args()

	var mounts []string
	for i, arg := range args {
		if arg == "--mount" {
			mounts = append(mounts, args[i+1])
		}
		if arg == "--volume" || arg == "-v" {
			t.Fatalf("-v creates a missing bind source; every mount is --mount: %v", args)
		}
	}
	for _, volume := range Volumes {
		if !slices.Contains(mounts, "type=volume,source="+volume.Name+",target="+volume.Target) {
			t.Errorf("volume %s is not mounted at %s: %v", volume.Name, volume.Target, mounts)
		}
	}
	env := "type=bind,source=" + filepath.Join(WebSecretDir, AppEnvName) + ",target=/run/secrets/" + AppEnvName + ",readonly"
	if !slices.Contains(mounts, env) {
		t.Errorf("the environment is not bound read-only: %v", mounts)
	}
}

func TestOnlyADigestPinnedImageIsRun(t *testing.T) {
	for _, ref := range []string{
		"ghcr.io/example/hq@" + digest,
		"ghcr.io/example/hq/composition:fp-1@" + digest,
		"localhost:5000/hq@" + digest,
	} {
		if _, err := Pinned(ref); err != nil {
			t.Errorf("%s refused: %v", ref, err)
		}
	}
	for _, ref := range []string{
		"ghcr.io/example/hq:latest",
		"ghcr.io/example/hq@sha256:short",
		"ghcr.io/example/hq@" + digest + " --privileged",
		"--privileged",
		"",
	} {
		if _, err := Pinned(ref); err == nil {
			t.Errorf("%q accepted", ref)
		}
	}
}

func TestTheRenderedEnvironmentIsTheRenderersFile(t *testing.T) {
	uid := os.Getuid()
	dir := filepath.Join(t.TempDir(), "web")

	if err := Rendered(dir, uid, uid); err == nil {
		t.Fatal("a missing directory was accepted")
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := Rendered(dir, uid, uid); err == nil {
		t.Fatal("a missing file was accepted")
	}

	// What docker leaves at a missing bind source: a directory at the name.
	file := filepath.Join(dir, AppEnvName)
	if err := os.Mkdir(file, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Rendered(dir, uid, uid); err == nil {
		t.Fatal("a directory at the file's name was accepted")
	}
	os.Remove(file)

	if err := os.WriteFile(file, []byte("A=1\n"), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := Rendered(dir, uid, uid); err != nil {
		t.Fatalf("the rendered file was refused: %v", err)
	}
	if err := Rendered(dir, uid, uid+1); err == nil {
		t.Error("a file another account owns was accepted")
	}
	if err := os.Link(file, filepath.Join(dir, "second")); err != nil {
		t.Fatal(err)
	}
	if err := Rendered(dir, uid, uid); err == nil {
		t.Error("a file with a second name was accepted")
	}
	os.Remove(filepath.Join(dir, "second"))

	if err := os.Chmod(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Rendered(dir, uid, uid); err == nil {
		t.Error("a directory others can enter was accepted")
	}
}

func TestNotifySpeaksTheProtocol(t *testing.T) {
	if err := Notify("", "READY=1"); err != nil {
		t.Errorf("no service manager is not an error: %v", err)
	}
	if err := Notify("relative", "READY=1"); err == nil {
		t.Error("a relative socket was accepted")
	}

	path := filepath.Join(t.TempDir(), "notify")
	listener, err := net.ListenUnixgram("unixgram", &net.UnixAddr{Name: path, Net: "unixgram"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := Notify(path, "READY=1"); err != nil {
		t.Fatal(err)
	}
	buffer := make([]byte, 64)
	n, _, err := listener.ReadFromUnix(buffer)
	if err != nil {
		t.Fatal(err)
	}
	if string(buffer[:n]) != "READY=1" {
		t.Errorf("sent %q", buffer[:n])
	}
}

func TestOnlyAShorterHealthStartPeriodIsTaken(t *testing.T) {
	image := "ghcr.io/example/hq@" + digest
	for given, want := range map[string]string{"": HealthStartPeriod.String(), "20s": "20s"} {
		period, err := StartPeriod(given)
		if err != nil {
			t.Fatalf("%q refused: %v", given, err)
		}
		args := strings.Join(Web{Image: image, StartPeriod: period}.Args(), " ")
		if !strings.Contains(args, "--health-start-period "+want+" ") {
			t.Errorf("%q: the container is not given a %s start period: %s", given, want, args)
		}
	}
	// Longer than the unit's start timeout allows, or no span at all.
	for _, given := range []string{"2m", "0s", "-5s", "soon"} {
		if _, err := StartPeriod(given); err == nil {
			t.Errorf("%q taken", given)
		}
	}
}
