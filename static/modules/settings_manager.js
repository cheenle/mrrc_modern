/**
 * FT-710 Settings Manager
 * ========================
 * Cookie persistence for all user preferences (settings object, AF volume,
 * scope display options, memory channels). Handles auth token reading for
 * WebSocket connections. One-time migration moves legacy localStorage /
 * sessionStorage values into cookies on first load.
 */

(() => {
    const SETTINGS_KEY = 'ft710_user_settings';
    const COOKIE_MAX_AGE = 365 * 24 * 60 * 60; // 1 year
    const DEFAULT_SETTINGS = {
        callsign: '',
        tuneStep: 1000,
        afGain: 128,
        rfPower: 100,
        lastFreq: 14200000,
        lastMode: 'USB',
        lastBand: '20m',
    };

    // Legacy keys migrated from web storage to cookies on first load.
    const LEGACY_LOCAL_KEYS = [
        SETTINGS_KEY,
        'ft710_afVol',
        'ft710_scopeFloor',
        'ft710_scopeCeil',
        'ft710_scopeTheme',
        'ft710_memChannels',
    ];
    const LEGACY_SESSION_KEYS = ['ft710_memChannels'];

    let settings = {};

    // ── Cookie Helpers ────────────────────────────────────────────
    function setCookie(name, value) {
        try {
            document.cookie =
                name +
                '=' +
                encodeURIComponent(value) +
                ';max-age=' +
                COOKIE_MAX_AGE +
                ';path=/;SameSite=Lax';
        } catch (e) {}
    }

    function getCookie(name) {
        const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
        return match ? decodeURIComponent(match[1]) : null;
    }

    // ── Legacy web-storage migration ──────────────────────────────
    function migrateKey(name, storage) {
        try {
            const v = storage.getItem(name);
            if (v !== null && getCookie(name) === null) setCookie(name, v);
            if (v !== null) storage.removeItem(name);
        } catch (e) {}
    }

    function migrateLegacyStorage() {
        LEGACY_LOCAL_KEYS.forEach((k) => {
            migrateKey(k, localStorage);
        });
        LEGACY_SESSION_KEYS.forEach((k) => {
            migrateKey(k, sessionStorage);
        });
    }

    // ── Settings object ───────────────────────────────────────────
    function load() {
        try {
            const raw = getCookie(SETTINGS_KEY);
            if (raw) {
                settings = Object.assign({}, DEFAULT_SETTINGS, JSON.parse(raw));
            } else {
                settings = Object.assign({}, DEFAULT_SETTINGS);
            }
        } catch (e) {
            settings = Object.assign({}, DEFAULT_SETTINGS);
        }
        return settings;
    }

    function save(newSettings) {
        Object.assign(settings, newSettings);
        setCookie(SETTINGS_KEY, JSON.stringify(settings));
    }

    function get(key, defaultValue) {
        return Object.hasOwn(settings, key) ? settings[key] : defaultValue;
    }

    // ── Auth Cookie Helper ────────────────────────────────────────
    function getAuthToken() {
        return getCookie('mrrc_auth') || '';
    }

    // ── Base path (raised-path deployments) ──────────────────────
    // Normally the UI is served from the root (`http://host:8888/`), the base is
    // empty, and every URL below is exactly what it always was. It can also be
    // served behind a path prefix — `https://edge/mrrc_modern/<name>/...` — which
    // is how the Cloud Hub can front an instance on a standard port with a trusted
    // certificate instead of a bare non-standard one. Without this, the absolute
    // `/WSradio` and `/api/...` URLs would escape the prefix and reach the edge
    // instead of the instance.
    //
    // Derivation follows the listen page (static/listen.js URL_BASE): the
    // directory part of the current path. A trailing slash, `/index.html` and a
    // bare route like `/login` all resolve to the same base.
    function basePath() {
        var p = window.location.pathname || '/';
        if (p.endsWith('/')) return p.slice(0, -1);
        var seg = p.slice(p.lastIndexOf('/') + 1);
        if (seg === '' || seg.indexOf('.') >= 0) {
            return p.slice(0, p.lastIndexOf('/'));
        }
        return p;
    }

    function url(path) {
        return basePath() + path;
    }

    // ── Initialize ────────────────────────────────────────────────
    migrateLegacyStorage();
    load();

    window.FT710Settings = {
        get: get,
        save: save,
        load: load,
        getAll: () => Object.assign({}, settings),
        getAuthToken: getAuthToken,
        setCookie: setCookie,
        getCookie: getCookie,
        basePath: basePath,
        url: url,
    };

    console.log('Settings Manager initialized');
})();
