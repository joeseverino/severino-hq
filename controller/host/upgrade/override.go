package upgrade

import "strings"

// pin is the override with the service's image set to image: replaced where
// the service names one, added where it does not, with the service or
// services: added when missing. Every other line is kept as it is. Block
// style only: a document written as one flow mapping, JSON among them, has no
// line this can add to, and is refused.
func pin(override, service, image string) (string, bool) {
	p := pinner{service: service, image: image}
	lines := strings.Split(override, "\n")
	if strings.HasSuffix(override, "\n") || override == "" {
		lines = lines[:len(lines)-1]
	}
	for _, line := range lines {
		if !p.line(line) {
			return "", false
		}
	}
	p.end()
	return p.out.String(), true
}

type pinner struct {
	service, image              string
	done, inServices, inService bool
	serviceWidth, childWidth    int
	out                         strings.Builder
}

func (p *pinner) print(line string) { p.out.WriteString(line + "\n") }

func (p *pinner) emitImage(width int) {
	p.print(strings.Repeat(" ", width) + "image: " + p.image)
	p.done = true
}

func (p *pinner) emitService(width int) {
	p.print(strings.Repeat(" ", width) + `"` + p.service + `":`)
	p.emitImage(width * 2)
}

func widthOr(width, fallback int) int {
	if width != 0 {
		return width
	}
	return fallback
}

// line takes one line of the override; false refuses the whole document.
func (p *pinner) line(line string) bool {
	if p.done || blank(line) {
		p.print(line)
		return true
	}
	width := indent(line)
	if !p.inServices {
		if trimmed := line[width:]; strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") {
			return false
		}
		if width == 0 && names(line, "services") {
			if !bare(line, "services") {
				return false
			}
			p.inServices = true
		}
		p.print(line)
		return true
	}
	if width == 0 {
		if p.inService {
			p.emitImage(widthOr(p.childWidth, p.serviceWidth*2))
		} else {
			p.emitService(widthOr(p.serviceWidth, 2))
		}
		p.print(line)
		return true
	}
	if p.serviceWidth == 0 {
		p.serviceWidth = width
	}
	if !p.inService {
		if width == p.serviceWidth && names(line, p.service) {
			if !bare(line, p.service) {
				return false
			}
			p.inService = true
		}
		p.print(line)
		return true
	}
	if p.childWidth == 0 {
		if width <= p.serviceWidth {
			p.emitImage(p.serviceWidth * 2)
			p.print(line)
			return true
		}
		p.childWidth = width
	}
	switch {
	case width < p.childWidth:
		p.emitImage(p.childWidth)
		p.print(line)
	case width == p.childWidth && strings.HasPrefix(line[width:], "image:"):
		p.emitImage(p.childWidth)
	default:
		p.print(line)
	}
	return true
}

func (p *pinner) end() {
	switch {
	case p.done:
	case !p.inServices:
		p.print("services:")
		p.emitService(2)
	case !p.inService:
		p.emitService(widthOr(p.serviceWidth, 2))
	default:
		p.emitImage(widthOr(p.childWidth, p.serviceWidth*2))
	}
}

func indent(line string) int { return len(line) - len(strings.TrimLeft(line, " ")) }

// blank is an empty line, spaces only, or a comment.
func blank(line string) bool {
	trimmed := strings.TrimLeft(line, " ")
	return trimmed == "" || strings.HasPrefix(trimmed, "#")
}

// key is what follows `name:` when line names that key, quoted or not.
func key(line, name string) (string, bool) {
	rest := strings.TrimPrefix(strings.TrimLeft(line, " "), `"`)
	if rest, ok := strings.CutPrefix(rest, name); ok {
		return strings.CutPrefix(strings.TrimPrefix(rest, `"`), ":")
	}
	return "", false
}

func names(line, name string) bool {
	_, ok := key(line, name)
	return ok
}

// bare is a key with no value on its line, so its children follow.
func bare(line, name string) bool {
	rest, ok := key(line, name)
	return ok && blank(rest)
}
