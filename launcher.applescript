-- Resolve Splash Patcher: abre la interfaz sin mostrar Terminal
on run
	set appPath to POSIX path of (path to me)
	set binPath to appPath & "Contents/Resources/ResolveSplashPatcher"
	try
		do shell script "nohup " & quoted form of binPath & " --background >/dev/null 2>&1 &"
	on error errMsg
		display dialog "No se pudo iniciar Resolve Splash Patcher:" & return & errMsg buttons {"OK"} default button "OK" with icon stop
	end try
end run
