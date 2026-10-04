package install

import (
	"bufio"
	"bytes"
	"strconv"
	"strings"
)

// MountChecker says whether a directory is backed by memory that never pages
// to disk. Nil error is the only yes.
type MountChecker func(dir string) error

// unescape undoes mountinfo's octal escapes (\040 for a space).
func unescape(field string) string {
	var out strings.Builder
	for i := 0; i < len(field); i++ {
		if field[i] == '\\' && i+3 < len(field) {
			if code, err := strconv.ParseUint(field[i+1:i+4], 8, 8); err == nil {
				out.WriteByte(byte(code))
				i += 3
				continue
			}
		}
		out.WriteByte(field[i])
	}
	return out.String()
}

// within reports whether dir is mount or lies under it.
func within(dir, mount string) bool {
	return dir == mount || mount == "/" || strings.HasPrefix(dir, mount+"/")
}

// CheckMountInfo reads /proc/self/mountinfo's text. Every mount stacked at the
// innermost mount point holding dir must be tmpfs, and the one on top must
// carry noswap: a plain tmpfs pages its contents out to swap. An answer that
// cannot be read is a refusal, never a yes.
func CheckMountInfo(mountinfo []byte, dir string) error {
	type mount struct {
		point, fstype string
		options       []string
	}
	var innermost []mount
	scanner := bufio.NewScanner(bytes.NewReader(mountinfo))
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		separator := -1
		for index, field := range fields {
			if field == "-" {
				separator = index
				break
			}
		}
		// "id parent major:minor root point options [optional...] - fstype source super"
		if separator < 6 || len(fields) < separator+4 {
			continue
		}
		found := mount{point: unescape(fields[4]), fstype: fields[separator+1],
			options: append(strings.Split(fields[5], ","), strings.Split(fields[separator+3], ",")...)}
		if !within(dir, found.point) {
			continue
		}
		switch {
		case len(innermost) == 0 || len(found.point) > len(innermost[0].point):
			innermost = []mount{found}
		case found.point == innermost[0].point:
			innermost = append(innermost, found)
		}
	}
	if scanner.Err() != nil || len(innermost) == 0 {
		return hostError("Controller secret directory must be on tmpfs.")
	}
	for _, each := range innermost {
		if each.fstype != "tmpfs" {
			return hostError("Controller secret directory must be on tmpfs.")
		}
	}
	for _, option := range innermost[len(innermost)-1].options {
		if option == "noswap" {
			return nil
		}
	}
	return hostError("Controller secret directory must be a tmpfs mounted with noswap.")
}
