-- mdlive.app -- the macOS document handler for Markdown files.
--
-- macOS delivers a double-clicked document to an application through an Apple
-- Event ('odoc'), not through argv, so a plain shell script cannot be a
-- document handler. An AppleScript applet can: its `on open` handler receives
-- the file list. All this applet does is hand the path to the real mdlive CLI
-- and quit; the CLI decides whether to start a server or reuse a running one.
--
-- The path below is substituted by install.sh, because `do shell script` runs a
-- non-interactive shell that never sources ~/.zshrc and so has no user PATH.

property mdlivePath : "__MDLIVE_PATH__"

on run
	-- Launched with no document, e.g. from Spotlight or the Dock. A document
	-- viewer with nothing to view should ask what to open.
	try
		set chosen to choose file with prompt "Choose a Markdown file to view" ¬
			of type {"net.daringfireball.markdown", "md", "markdown", "mdown", "mkd"}
		viewFile(POSIX path of chosen)
	on error number -128
		-- User cancelled. Quitting silently is the correct response.
	end try
end run

on open theFiles
	set total to count of theFiles
	repeat with index from 1 to total
		viewFile(POSIX path of (item index of theFiles))
		-- Opening several files at once would otherwise race: each invocation
		-- checks the instance registry before any of them has written to it,
		-- and they all start their own server. A short stagger lets the first
		-- one register so the rest reuse it.
		if index < total then delay 0.8
	end repeat
end open

on viewFile(posixPath)
	-- nohup and a trailing & so the applet can exit immediately while the
	-- server keeps running, reparented to launchd.
	do shell script "/usr/bin/nohup " & quoted form of mdlivePath & " " & ¬
		quoted form of posixPath & " >/dev/null 2>&1 &"
end viewFile
