//go:build !windows

package main

import "os"

func linkedEntry(info os.FileInfo) bool { return info.Mode()&os.ModeSymlink != 0 }

// There is deliberately no non-Windows production deletion entrypoint.
func main() {}
