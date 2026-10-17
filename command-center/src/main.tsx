import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Toaster } from 'sonner'
import { SessionProvider } from './lib/session'
import { Layout } from './components/Layout'
import Dashboard from './pages/Dashboard'
import Tickets from './pages/Tickets'
import TicketDetail from './pages/TicketDetail'
import Requests from './pages/Requests'
import RequestDetail from './pages/RequestDetail'
import Runs from './pages/Runs'
import RunDetail from './pages/RunDetail'
import Approvals from './pages/Approvals'
import Definitions from './pages/Definitions'
import DefinitionDetail from './pages/DefinitionDetail'
import Audit from './pages/Audit'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <SessionProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/tickets" element={<Tickets />} />
            <Route path="/tickets/:id" element={<TicketDetail />} />
            <Route path="/requests" element={<Requests />} />
            <Route path="/requests/:id" element={<RequestDetail />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/definitions" element={<Definitions />} />
            <Route path="/definitions/:key" element={<DefinitionDetail />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<div className="card p-6">Page not found.</div>} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </SessionProvider>
    <Toaster richColors position="top-right" />
  </StrictMode>,
)
