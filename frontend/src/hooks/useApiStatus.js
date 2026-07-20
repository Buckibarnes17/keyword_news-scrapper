import { useEffect, useState } from 'react'
import { api } from '../api/client'
import useAppStore from '../store/appStore'

export function useApiStatus() {
  const { setApiConnected } = useAppStore()
  const [clock, setClock] = useState('')

  useEffect(() => {
    // Live clock
    const tick = () => {
      const n = new Date()
      const p = v => String(v).padStart(2, '0')
      setClock(`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`)
    }
    tick()
    const cId = setInterval(tick, 1000)

    // API health check every 30s
    const check = async () => {
      try {
        await api.getHealth()
        setApiConnected(true)
      } catch (err) {
        setApiConnected(false)
      }
    }
    check()
    const hId = setInterval(check, 30000)

    return () => {
      clearInterval(cId)
      clearInterval(hId)
    }
  }, [])

  return clock
}
