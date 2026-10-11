// SPDX-License-Identifier: MIT
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static BOOL game_is_foreground(void) {
    DWORD pid = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &pid);
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return FALSE;
    WCHAR path[32768], expected[32768];
    DWORD count = 32768;
    BOOL read = QueryFullProcessImageNameW(process, 0, path, &count);
    CloseHandle(process);
    DWORD size = GetEnvironmentVariableW(L"BF4_EXPECTED_EXE", expected, 32768);
    return read && size > 0 && size < 32768 && !_wcsicmp(path, expected);
}

static void send_button(int button, int down) {
    if (!game_is_foreground()) return;
    INPUT input = {0};
    input.type = INPUT_MOUSE;
    input.mi.mouseData = button == 3 ? XBUTTON1 : XBUTTON2;
    input.mi.dwFlags = down ? MOUSEEVENTF_XDOWN : MOUSEEVENTF_XUP;
    input.mi.dwExtraInfo = 0x4246344d;
    SendInput(1, &input, sizeof(input));
}

int main(void) {
    char line[32]; unsigned held = 0;
    while (fgets(line, sizeof(line), stdin)) {
        int button, down;
        if (sscanf(line, "%d %d", &button, &down) != 2 ||
            (button != 3 && button != 4) || (down != 0 && down != 1)) continue;
        send_button(button, down);
        if (down) held |= 1u << button; else held &= ~(1u << button);
    }
    if (held & (1u << 3)) send_button(3, 0);
    if (held & (1u << 4)) send_button(4, 0);
    return 0;
}
