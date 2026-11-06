// Package atomicfile puts a file in place whole: a reader, or the disk after a
// power cut, holds the old file or the new one, never part of either. The
// file is written beside its destination, synced, renamed or linked over it,
// and the directory is synced so the rename itself survives.
//
// Neither the standard library nor github.com/google/renameio/v2 does all of
// that: os.Root.WriteFile truncates in place, and renameio syncs the file but
// never its directory.
package atomicfile

import (
	"crypto/rand"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

// File is a file being written beside its destination. Replace or Link puts it
// in place; Discard, deferred, removes it when neither did.
type File struct {
	*os.File
	root   *os.Root
	temp   string
	target string
	done   bool
}

// Create starts a file for path, in path's directory, with mode set before
// anything is written.
func Create(path string, mode fs.FileMode) (*File, error) {
	return create(nil, path, mode)
}

// CreateIn is Create for name beneath root, which no part of the write
// leaves.
func CreateIn(root *os.Root, name string, mode fs.FileMode) (*File, error) {
	return create(root, name, mode)
}

// create stages path's file beside it, beneath root when root is set.
func create(root *os.Root, path string, mode fs.FileMode) (*File, error) {
	open := os.OpenFile
	if root != nil {
		open = root.OpenFile
	}
	temp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+"."+rand.Text())
	file, err := open(temp, os.O_RDWR|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, err
	}
	f := &File{File: file, root: root, temp: temp, target: path}
	if err := f.Chmod(mode); err != nil {
		f.Discard()
		return nil, err
	}
	return f, nil
}

// Temporary is the written file's path, for a program that checks it before
// it is put in place (relative to the root for CreateIn).
func (f *File) Temporary() string { return f.temp }

// Replace puts the file at its destination, over whatever is there.
func (f *File) Replace() error {
	rename := os.Rename
	if f.root != nil {
		rename = f.root.Rename
	}
	return f.commit(rename)
}

// Link puts the file at its destination only when nothing is there yet, and
// fails otherwise.
func (f *File) Link() error {
	link := os.Link
	if f.root != nil {
		link = f.root.Link
	}
	return f.commit(func(temp, target string) error {
		if err := link(temp, target); err != nil {
			return err
		}
		f.remove()
		return nil
	})
}

func (f *File) commit(place func(temp, target string) error) error {
	if err := f.Sync(); err != nil {
		f.Discard()
		return err
	}
	f.done = true
	if err := f.Close(); err != nil {
		f.remove()
		return err
	}
	if err := place(f.temp, f.target); err != nil {
		f.remove()
		return err
	}
	if f.root != nil {
		return syncIn(f.root, filepath.Dir(f.target))
	}
	return SyncDir(filepath.Dir(f.target))
}

// Discard closes and removes the file unless it was put in place.
func (f *File) Discard() {
	if f.done {
		return
	}
	f.done = true
	f.Close()
	f.remove()
}

func (f *File) remove() {
	if f.root != nil {
		f.root.Remove(f.temp)
		return
	}
	os.Remove(f.temp)
}

// WriteFile replaces path with data, readable by mode.
func WriteFile(path string, data []byte, mode fs.FileMode) error {
	file, err := Create(path, mode)
	if err != nil {
		return err
	}
	return write(file, data)
}

// WriteFileIn replaces name beneath root with data, readable by mode.
func WriteFileIn(root *os.Root, name string, data []byte, mode fs.FileMode) error {
	file, err := CreateIn(root, name, mode)
	if err != nil {
		return err
	}
	return write(file, data)
}

func write(file *File, data []byte) error {
	if _, err := file.Write(data); err != nil {
		file.Discard()
		return err
	}
	return file.Replace()
}

// SyncDir makes the names in dir durable: a rename into it, or out of it,
// survives a power cut once it returns.
func SyncDir(dir string) error {
	handle, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer handle.Close()
	return handle.Sync()
}

func syncIn(root *os.Root, dir string) error {
	handle, err := root.Open(dir)
	if err != nil {
		return err
	}
	defer handle.Close()
	return handle.Sync()
}
