import { create } from 'zustand'
import { persist } from 'zustand/middleware'

const useAppStore = create(
  persist(
    (set, get) => ({
      // Navigation
      activeView: 'dashboard',        // dashboard|new-crawl|results|history|schedules|config-manager
      setActiveView: (v) => set({ activeView: v }),

      // Authentication
      user: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      authView: 'login', // 'login' | 'signup'
      setAuthView: (view) => set({ authView: view }),
      
      login: async (accessToken, refreshToken, user) => {
        localStorage.setItem('kws_access_token', accessToken)
        localStorage.setItem('kws_refresh_token', refreshToken)
        localStorage.setItem('kws_user', JSON.stringify(user))
        set({
          accessToken,
          refreshToken,
          user,
          isAuthenticated: true,
          authView: 'login'
        })
      },
      
      logout: async () => {
        localStorage.removeItem('kws_access_token')
        localStorage.removeItem('kws_refresh_token')
        localStorage.removeItem('kws_user')
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          isAuthenticated: false,
          activeView: 'dashboard',
          authView: 'login'
        })
        try {
          const { api } = await import('../api/client')
          await api.logout()
        } catch (e) {
          // ignore
        }
      },

      hydrateFromStorage: async () => {
        const accessToken = localStorage.getItem('kws_access_token')
        const refreshToken = localStorage.getItem('kws_refresh_token')
        const userStr = localStorage.getItem('kws_user')
        if (accessToken && refreshToken) {
          try {
            const user = userStr ? JSON.parse(userStr) : null
            set({
              accessToken,
              refreshToken,
              user,
              isAuthenticated: true
            })
            // Fetch fresh user profile on startup to verify token validity
            const { api } = await import('../api/client')
            const freshUser = await api.me()
            set({ user: freshUser })
            localStorage.setItem('kws_user', JSON.stringify(freshUser))
          } catch (e) {
            console.warn('Hydration token verification failed, logging out...', e)
            get().logout()
          }
        }
      },

      // New crawl tab mode
      crawlTab: 'search',             // search|direct|config
      setCrawlTab: (t) => set({ crawlTab: t }),

      // Active crawl job
      activeSearchId: null,
      activeSearchKeyword: '',
      searchResults: [],
      searchMeta: null,
      pollingErrors: 0,

      setActiveSearch: (id, kw) => set({
        activeSearchId: id,
        activeSearchKeyword: kw,
        searchResults: [],
        searchMeta: null,
        pollingErrors: 0
      }),
      setSearchPollData: (meta, items) => set({ searchMeta: meta, searchResults: items }),
      incPollingErrors: () => set(s => ({ pollingErrors: s.pollingErrors + 1 })),
      clearPollingErrors: () => set({ pollingErrors: 0 }),

      // Results filters (client-side only)
      filters: {
        searchQuery: '',
        status: '',
        excludeDuplicates: true,
        minRelevance: 0,
        page: 1,
        limit: 50,
        sortBy: 'relevance',
        sortDesc: true
      },
      setFilters: (patch) => set(s => ({ filters: { ...s.filters, ...patch, page: 1 } })),
      setPage: (p) => set(s => ({ filters: { ...s.filters, page: p } })),

      // History and schedules
      historyList: [],
      scheduleList: [],
      setHistoryList: (l) => set({ historyList: l }),
      setScheduleList: (l) => set({ scheduleList: l }),
      schedulePrefill: null,
      setSchedulePrefill: (data) => set({ schedulePrefill: data }),
      newCrawlKeyword: '',
      setNewCrawlKeyword: (kw) => set({ newCrawlKeyword: kw }),

      // Config state
      urlsConfig: null,                      // full urls.json object
      keywordsConfig: null,                  // full keywords.json object
      configSelected: new Set(),             // selected URL strings for Config File tab
      configSelectedKeywords: new Set(),     // selected keyword strings for Config File tab
      setUrlsConfig: (d) => set({ urlsConfig: d }),
      setKeywordsConfig: (d) => set({ keywordsConfig: d }),
      setConfigSelected: (s) => set({ configSelected: s }),
      setConfigSelectedKeywords: (s) => set({ configSelectedKeywords: s }),

      // API connection
      apiConnected: true,
      setApiConnected: (v) => set({ apiConnected: v }),

      // Tor state
      torEnabled: false,
      torReachable: false,
      torProxyUrl: null,
      setTorState: (patch) => set(patch),
    }),
    {
      name: 'kws-history',
      partialize: (state) => ({ historyList: state.historyList }),
      partialState: (state) => ({ historyList: state.historyList })
    }
  )
)

export default useAppStore
