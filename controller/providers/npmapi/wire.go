package npmapi

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
)

// Flag is an NPM boolean. NPM answers true/false or 1/0 depending on its
// release and database; anything else is not a flag.
type Flag bool

func (f *Flag) UnmarshalJSON(data []byte) error {
	switch string(bytes.TrimSpace(data)) {
	case "true", "1":
		*f = true
	case "false", "0":
		*f = false
	default:
		return fmt.Errorf("npm flag %s is not true, false, 1 or 0", data)
	}
	return nil
}

// ID is an NPM id: a whole number, answered as a number or a decimal string.
type ID int

func (i *ID) UnmarshalJSON(data []byte) error {
	var text string
	if json.Unmarshal(data, &text) != nil {
		text = string(bytes.TrimSpace(data))
	}
	value, err := strconv.Atoi(text)
	if err != nil {
		return fmt.Errorf("npm id %s is not a whole number", data)
	}
	*i = ID(value)
	return nil
}

func (i ID) String() string { return strconv.Itoa(int(i)) }
