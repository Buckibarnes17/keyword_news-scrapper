import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import useAppStore from '../store/appStore'

const POLL_INTERVAL = 3000
const MAX_ERRORS = 5

export function usePolling() {
  const { activeSearchId, setSearchPollData, incPollingErrors,
          clearPollingErrors } = useAppStore()
  const ref = useRef(null)

  useEffect(() => {
    if (!activeSearchId) return
    const poll = async () => {
      try {
        const data = await api.getResults(activeSearchId)
        setSearchPollData(data.search_meta, data.results.items)
        clearPollingErrors()
        if (['completed','failed','stopped'].includes(data.search_meta.status)) {
          clearInterval(ref.current)
        }
      } catch (err) {
        incPollingErrors()
        const currentErrors = useAppStore.getState().pollingErrors
        if (currentErrors >= MAX_ERRORS) {
          clearInterval(ref.current)
        }
      }
    }
    poll()
    ref.current = setInterval(poll, POLL_INTERVAL)
    return () => clearInterval(ref.current)
  }, [activeSearchId])
}
