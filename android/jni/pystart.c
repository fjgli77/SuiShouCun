/* 随手存 · Android 内嵌 CPython 启动器（JNI）
 *
 * 把 Chaquopy 提供的 libpython3.13.so 链接进 APK，在 Java 后台线程里
 * 初始化解释器并执行 media_dl.android_entry.main()（Flask 本机服务）。
 * 不使用 exec()：Android 10+ 禁止执行应用私有目录里的可执行文件，
 * 因此走 JNI 嵌入而不是启动独立进程。
 */
#include <jni.h>
#include <Python.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void release_chars(JNIEnv *env, jstring s, const char *p) {
    if (s && p) (*env)->ReleaseStringUTFChars(env, s, p);
}

JNIEXPORT jint JNICALL
Java_com_suishoucun_app_PyService_nativeStart(JNIEnv *env, jclass cls,
                                              jstring jHome,
                                              jstring jPaths,
                                              jstring jScript) {
    (void) cls;
    const char *home = (*env)->GetStringUTFChars(env, jHome, NULL);
    const char *paths = (*env)->GetStringUTFChars(env, jPaths, NULL);
    const char *script = (*env)->GetStringUTFChars(env, jScript, NULL);
    if (!home || !paths || !script) {
        release_chars(env, jHome, home);
        release_chars(env, jPaths, paths);
        release_chars(env, jScript, script);
        return -2;
    }

    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONPATH", paths, 1);
    setenv("MEDIADL_HOME", home, 1);
    setenv("MEDIADL_RUNTIME", "android", 1);
    setenv("PYTHONIOENCODING", "utf-8", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);

    /* 移动端不需要信号处理器 / site / .pyc；stdio 用 UTF-8 避免 locale 问题 */
    config.install_signal_handlers = 0;
    config.site_import = 0;
    config.user_site_directory = 0;
    config.write_bytecode = 0;
    config.parse_argv = 0;
    config.buffered_stdio = 1;
    config.pathconfig_warnings = 0;
    config.module_search_paths_set = 1;

    status = PyConfig_SetBytesString(&config, &config.program_name, "python3");
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.home, home);
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.stdio_encoding, "utf-8");
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.filesystem_encoding, "utf-8");

    /* paths 以 ':' 分隔：stdlib.zip、应用源码、纯 Python 依赖、原生扩展目录 */
    char *copy = strdup(paths);
    char *saveptr = NULL;
    for (char *part = strtok_r(copy, ":", &saveptr);
         part != NULL && !PyStatus_Exception(status);
         part = strtok_r(NULL, ":", &saveptr)) {
        if (*part == '\0') continue;
        wchar_t *wide = Py_DecodeLocale(part, NULL);
        if (!wide) continue;
        status = PyWideStringList_Append(&config.module_search_paths, wide);
        PyMem_RawFree(wide);
    }
    free(copy);

    if (PyStatus_Exception(status)) {
        fprintf(stderr, "media_dl: PyConfig failed: %s\n",
                status.err_msg ? status.err_msg : "unknown error");
        PyConfig_Clear(&config);
        release_chars(env, jHome, home);
        release_chars(env, jPaths, paths);
        release_chars(env, jScript, script);
        return -3;
    }

    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "media_dl: Py_InitializeFromConfig failed: %s\n",
                status.err_msg ? status.err_msg : "unknown error");
        release_chars(env, jHome, home);
        release_chars(env, jPaths, paths);
        release_chars(env, jScript, script);
        return -4;
    }

    /* 在 __main__ 里启动 Flask；调用会一直阻塞直到进程结束 */
    int rc = PyRun_SimpleString(script);
    if (rc != 0) {
        PyErr_Print();
        fprintf(stderr, "media_dl: startup script failed\n");
    }
    Py_FinalizeEx();

    release_chars(env, jHome, home);
    release_chars(env, jPaths, paths);
    release_chars(env, jScript, script);
    return rc;
}
